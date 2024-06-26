#!/usr/bin/env python3.11
"""This script computes important variables but first passes arguments to preparation.py."""

import pandas as pd
import numpy as np
import argparse
import cProfile
import pstats
import time
import polars as pl
from datetime import timedelta
from datetime import datetime
from preparation import prepare_datasets

# Parse arguments
parser = argparse.ArgumentParser(
    description="Prepare datasets for trade sign analysis and variable estimation."
)
parser.add_argument(
    "hdf5_file_path", type=str, help="The path to the original HDF5 file."
)
parser.add_argument("base_date", type=str, help="Base date for the analysis.")
parser.add_argument("stock_name", type=str, help="Stock symbol.")
parser.add_argument("year", type=str, help="Year of the data.")
parser.add_argument("month", type=str, help="Month of the data.")
parser.add_argument("day", type=str, help="Day of the data.")
parser.add_argument(
    "ctm_dataset_path",
    type=str,
    help="The dataset path within the HDF5 file for ctm data.",
)
parser.add_argument(
    "complete_nbbo_dataset_path",
    type=str,
    help="The dataset path within the HDF5 file for complete nbbo data.",
)

args, unknown = parser.parse_known_args()

# Constructing file paths based on the arguments
hdf5_variable_path = f"/home/taq/taq_variables/{args.year}{args.month}_{args.stock_name}_variables.h5"
print(f"Output HDF5 file path: {hdf5_variable_path}")

def main():
    global aggregated_data
    aggregated_data = {}
    aggregated_data_outside_trading = {}
    trades, Buys_trades, Sells_trades, Ask, Bid, Retail_trades, Oddlot_trades, Midpoint, trade_returns, midprice_returns, trade_signs, nbbo_signs = prepare_datasets(
        args.hdf5_file_path,
        args.base_date,
        args.stock_name,
        args.year,
        args.month,
        args.day,
        args.ctm_dataset_path,
        args.complete_nbbo_dataset_path
    )

    # Start timing the main calculations
    main_start_time = time.time()

    #Customized Functions for calculating variables
      
    def calculate_minute_volatility(returns):
        n = len(returns)
        if n <= 1:
            return np.nan
        mean_return = returns.mean()
        return ((returns - mean_return) ** 2).sum() / (len(returns) - 1)

    def calculate_autocorrelation(returns):
        n = len(returns)
        if n <= 1:
            return np.nan
        mean = returns.mean()
        variance = ((returns - mean) ** 2).sum() / (n - 1)
        if np.isclose(variance, 0):
            return np.nan
        covariance = ((returns - mean) * (returns.shift(1) - mean)).sum() / (n - 1)
        return covariance / variance

    def calculate_voib_shr(df1, df2):
        if df1 is None or df1.empty or df1.isna().all().all() or df2 is None or df2.empty or df2.isna().all().all():
            return None
        
        if 'time' in df1.columns:
            df1.set_index('time', inplace=True)
        if 'time' in df2.columns:
            df2.set_index('time', inplace=True)

        df1_filtered = df1.between_time("09:30", "16:00")
        df2_filtered = df2.between_time("09:30", "16:00")
        
        if df1_filtered.empty or df2_filtered.empty:
            return None
        buys_per_s = df1_filtered.resample("s")["vol"].sum()
        sells_per_s = df2_filtered.resample("s")["vol"].sum()
        
        oib_shr_s = (buys_per_s - sells_per_s) / (buys_per_s + sells_per_s)

        return oib_shr_s

    def apply_voib_shr_aggregations(df):
        if df is None or df.empty or df.isna().all().all():
            return None
        
        pl_df = pl.from_pandas(df)

        resampled_df = pl_df.groupby_dynamic('time', every='1m', closed='left').agg([
        pl.col('OIB_SHR').apply(calculate_minute_volatility, return_dtype=pl.Float64).alias('VOIB_SHR'),
        pl.col('OIB_SHR').apply(calculate_autocorrelation, return_dtype=pl.Float64).alias('OIB_SHR_autocorr'),
        ])
        return resampled_df.to_pandas().set_index('time')
            

    #below fix the names
          
    def apply_return_aggregations(pl_df, column='returns'):
        if pl_df is None or pl_df.shape[0] == 0 or pl_df.select(pl.col(column).is_null().any()).item():
            return None

        # Perform dynamic grouping and calculate volatility and autocorrelation
        resampled_df = pl_df.groupby_dynamic('time', every='1m', closed='left').agg([
            pl.col(column).apply(calculate_minute_volatility, return_dtype=pl.Float64).alias('trade_ret_volatility'),
            pl.col(column).apply(calculate_autocorrelation, return_dtype=pl.Float64).alias('trade_ret_autocorr'),
        ])
        return resampled_df.to_pandas().set_index('time')
        
              
    def apply_return_aggregations_outside_trading(pl_df, column='returns'):
        if pl_df is None or pl_df.shape[0] == 0 or pl_df.select(pl.col(column).is_null().any()).item():
            return None

        # Perform dynamic grouping and calculate volatility and autocorrelation
        resampled_df = pl_df.groupby_dynamic('time', every='30m', closed='left').agg([
            pl.col(column).apply(calculate_minute_volatility, return_dtype=pl.Float64).alias('trade_ret_volatility'),
            pl.col(column).apply(calculate_autocorrelation, return_dtype=pl.Float64).alias('trade_ret_autocorr'),
        ])
        return resampled_df.to_pandas().set_index('time')

    
    def apply_ret_variances_aggregations(pl_df, column='returns'):
        if pl_df is None or pl_df.shape[0] == 0 or pl_df.select(pl.col(column).is_null().all()).item():
            return None
        resampled_df = pl_df.groupby_dynamic('time', every='1m', closed='left').agg([
        pl.col(column).apply(calculate_minute_volatility, return_dtype=pl.Float64).alias('variance')])
           
        return resampled_df.to_pandas().set_index('time')
    
    
    def reindex_to_full_time(df, base_date, outside_trading=False):
        if df is None or df.empty or df.isna().all().all():
            return None
        
        if outside_trading:
            morning_index = pd.date_range(start=f"{base_date} 04:00", end=f"{base_date} 09:29", freq="30min")
            evening_index = pd.date_range(start=f"{base_date} 16:01", end=f"{base_date} 20:00", freq="30min")
            full_time_index = morning_index.union(evening_index)
        else:
            full_time_index = pd.date_range(start=f"{base_date} 09:30", end=f"{base_date} 16:00", freq="1min")

        return df.reindex(full_time_index)
    

    # Aggregated Functions for Trades

    def apply_aggregations(df, df_name):
        if df is None or df.empty or df.isna().all().all():
            return None
        if len(df) == 1:
            return df
        if 'value' not in df.columns:
            df['value'] = df['price'] * df['vol']
        

        df_filtered = df[['price', 'vol', 'value']]
        df_filtered = df_filtered.between_time("09:29", "16:00")

        if df_filtered.empty or df_filtered.isna().all().all():
            return None
        
        df_filtered.reset_index(inplace = True)
        df_filtered['durations'] = df_filtered['time'].diff().fillna(pd.Timedelta(seconds=0)).dt.total_seconds()

        df_filtered['weighted_price'] = df_filtered['price'] * df_filtered['durations']
        pl_df = pl.from_pandas(df_filtered)


        try:
            seconds_df = pl_df.group_by_dynamic('time', every='1s', label='left').agg([
                pl.count('price').alias('count')
            ])

            max_trades_per_sec = seconds_df.group_by_dynamic('time', every='1m', label='left').agg([
                pl.col('count').max().alias(f'{df_name}_max_events_per_sec')
            ])

            def calculate_vwap_pl():
                return (pl.col('price') * pl.col('vol')).sum() / pl.col('vol').sum()

            def calculate_twap_pl():
                return (pl.sum('weighted_price') / pl.sum('durations'))
    
            aggregations = [
                pl.col('price').last().alias(f'{df_name}_last_price'),
                pl.col('vol').last().alias(f'{df_name}_last_vol'),
                pl.col('time').last().alias(f'{df_name}_last_time'),
                pl.col('price').mean().alias(f'{df_name}_avg_price'),
                pl.col('vol').mean().alias(f'{df_name}_avg_vol'),
                pl.col('vol').sum().alias(f'{df_name}_tot_vol'),
                calculate_vwap_pl().alias(f'{df_name}_vwap'),
                calculate_twap_pl().alias(f'{df_name}_twap'),
                pl.count('price').alias(f'{df_name}_num_events')
            ]

            resampled_df = pl_df.group_by_dynamic('time', every='1m', closed='left', label='left').agg(aggregations)


            resampled_df = resampled_df.join(max_trades_per_sec, on='time', how='inner')
            return resampled_df.to_pandas().set_index('time')
        
        except Exception as e:
            print(f"An error occurred: {e}")
            return None
   
   
    def apply_aggregations_outside_trading(df, df_name, base_date):
        if df is None or df.empty or df.isna().all().all():
            return None
        if len(df) == 1:
            return df
        if 'value' not in df.columns:
            df['value'] = df['price'] * df['vol']

        df_filtered = df[['price', 'vol', 'value']]

        start_time_morning = f"{base_date} 09:30"
        end_time_afternoon = f"{base_date} 16:00"
        df_filtered = df.loc[(df.index < start_time_morning) | (df.index > end_time_afternoon)]
        
        if df_filtered.empty or df_filtered.isna().all().all():
            return None
        
        df_filtered.reset_index(inplace = True)
        df_filtered['durations'] = df_filtered['time'].diff().fillna(pd.Timedelta(seconds=0)).dt.total_seconds()

        df_filtered['weighted_price'] = df_filtered['price'] * df_filtered['durations']
        pl_df = pl.from_pandas(df_filtered)

        try:
            seconds_df = pl_df.group_by_dynamic('time', every='1s', label='left').agg([
                pl.count('price').alias('count')
            ])

            max_trades_per_sec = seconds_df.group_by_dynamic('time', every='30m', label='left').agg([
                pl.col('count').max().alias(f'{df_name}_max_events_per_sec')
            ])

            def calculate_vwap_pl():
                return (pl.col('price') * pl.col('vol')).sum() / pl.col('vol').sum()
            
            def calculate_twap_pl():
                return (pl.sum('weighted_price') / pl.sum('durations'))
            
            aggregations = [
                pl.col('price').last().alias(f'{df_name}_last_price'),
                pl.col('vol').last().alias(f'{df_name}_last_vol'),
                pl.col('time').last().alias(f'{df_name}_last_time'),
                pl.col('price').mean().alias(f'{df_name}_avg_price'),
                pl.col('vol').mean().alias(f'{df_name}_avg_vol'),
                pl.col('vol').sum().alias(f'{df_name}_tot_vol'),
                calculate_vwap_pl().alias(f'{df_name}_vwap'),
                calculate_twap_pl().alias(f'{df_name}_twap'),
                pl.count('price').alias(f'{df_name}_num_events')
            ]

            resampled_df = pl_df.group_by_dynamic('time', every='30m', closed='left', label='left').agg(aggregations)

            resampled_df = resampled_df.join(max_trades_per_sec, on='time', how='inner')
            return resampled_df.to_pandas().set_index('time')
        
        except Exception as e:
            print(f"An error occurred: {e}")
            return None
    
    #Aggregated Functions for Quotes
    def apply_quote_aggregations(df, df_name):
        if df is None or df.empty or df.isna().all().all():
            return None
        if len(df) == 1:
            return df
        if 'value' not in df.columns:
            df['value'] = df['price'] * df['vol']

        df_filtered = df[['price', 'vol', 'value', 'qu_cond']]

        df_filtered = df.between_time("09:29", "16:00")

        if df_filtered.empty or df_filtered.isna().all().all():
            return None
        
        df_filtered.reset_index(inplace = True)
        df_filtered['durations'] = df_filtered['time'].diff().fillna(pd.Timedelta(seconds=0)).dt.total_seconds()

        df_filtered['weighted_price'] = df_filtered['price'] * df_filtered['durations']
        pl_df = pl.from_pandas(df_filtered)

        try:
            seconds_df = pl_df.group_by_dynamic('time', every='1s', label='left').agg([
                pl.count('price').alias('count')
            ])

            max_trades_per_sec = seconds_df.group_by_dynamic('time', every='1m', label='left').agg([
                pl.col('count').max().alias(f'{df_name}_max_events_per_sec')
            ])

            def calculate_vwap_pl():
                return (pl.col('price') * pl.col('vol')).sum() / pl.col('vol').sum()
            
            def calculate_twap_pl():
                return (pl.sum('weighted_price') / pl.sum('durations'))
            
            def encode_conditions_expr(column):
                conditions = {'D': 1, 'P': 2, 'J': 4, 'K': 8}
                
                encoded_col = pl.lit(0)
                
                for cond, code in conditions.items():
                    encoded_col += pl.when(pl.col(column).str.contains(cond)).then(pl.lit(code)).otherwise(pl.lit(0))
                
                return encoded_col.alias('encoded_conditions')
                        
            aggregations = [
                pl.col('price').last().alias(f'{df_name}_last_price'),
                pl.col('vol').last().alias(f'{df_name}_last_vol'),
                pl.col('time').last().alias(f'{df_name}_last_time'),
                pl.col('price').mean().alias(f'{df_name}_avg_price'),
                pl.col('vol').mean().alias(f'{df_name}_avg_vol'),
                pl.col('vol').sum().alias(f'{df_name}_tot_vol'),
                calculate_vwap_pl().alias(f'{df_name}_vwap'),
                calculate_twap_pl().alias(f'{df_name}_twap'),
                pl.count('price').alias(f'{df_name}_num_events'),
                encode_conditions_expr('qu_cond').sum().alias(f'{df_name}_qu_cond')
            ]

            resampled_df = pl_df.group_by_dynamic('time', every='1m', closed='left', label='left').agg(aggregations)
            resampled_df = resampled_df.join(max_trades_per_sec, on='time', how='inner')
            return resampled_df.to_pandas().set_index('time')
        
        except Exception as e:
            print(f"An error occurred: {e}")
            return None

    def apply_quote_aggregations_outside_trading(df, df_name, base_date):
        if df is None or df.empty or df.isna().all().all():
            return None
        if len(df) == 1:
            return df
        if 'value' not in df.columns:
            df['value'] = df['price'] * df['vol']

        df_filtered = df[['price', 'vol', 'value', 'qu_cond']]
        
        start_time_morning = f"{base_date} 09:30"
        end_time_afternoon = f"{base_date} 16:00"
        df_filtered = df.loc[(df.index < start_time_morning) | (df.index > end_time_afternoon)]

        if df_filtered.empty or df_filtered.isna().all().all():
            return None
        
        df_filtered.reset_index(inplace = True)
        df_filtered['durations'] = df_filtered['time'].diff().fillna(pd.Timedelta(seconds=0)).dt.total_seconds()

        df_filtered['weighted_price'] = df_filtered['price'] * df_filtered['durations']
        pl_df = pl.from_pandas(df_filtered) 

        try:
            seconds_df = pl_df.group_by_dynamic('time', every='1s', label='left').agg([
                pl.count('price').alias('count')
            ])

            max_trades_per_sec = seconds_df.group_by_dynamic('time', every='30m', label='left').agg([
                pl.col('count').max().alias(f'{df_name}_max_events_per_sec')
            ])

            def calculate_vwap_pl():
                return (pl.col('price') * pl.col('vol')).sum() / pl.col('vol').sum()
            
            def calculate_twap_pl():
                return (pl.sum('weighted_price') / pl.sum('durations'))
            
            def encode_conditions_expr(column):
                conditions = {'D': 1, 'P': 2, 'J': 4, 'K': 8}
                
                encoded_col = pl.lit(0)
                
                for cond, code in conditions.items():
                    encoded_col += pl.when(pl.col(column).str.contains(cond)).then(pl.lit(code)).otherwise(pl.lit(0))
                
                return encoded_col.alias('encoded_conditions')
                        
            aggregations = [
                pl.col('price').last().alias(f'{df_name}_last_price'),
                pl.col('vol').last().alias(f'{df_name}_last_vol'),
                pl.col('time').last().alias(f'{df_name}_last_time'),
                pl.col('price').mean().alias(f'{df_name}_avg_price'),
                pl.col('vol').mean().alias(f'{df_name}_avg_vol'),
                pl.col('vol').sum().alias(f'{df_name}_tot_vol'),
                calculate_vwap_pl().alias(f'{df_name}_vwap'),
                calculate_twap_pl().alias(f'{df_name}_twap'),
                pl.count('price').alias(f'{df_name}_num_events'),
                encode_conditions_expr('qu_cond').sum().alias(f'{df_name}_qu_cond')
            ]

            resampled_df = pl_df.group_by_dynamic('time', every='30m', closed='left', label='left').agg(aggregations)
            resampled_df = resampled_df.join(max_trades_per_sec, on='time', how='inner')
            return resampled_df.to_pandas().set_index('time')
        
        except Exception as e:
            print(f"An error occurred: {e}")
            return None
    
    #Functions for Midprice

    def apply_midpoint_aggregations(df):
        if df is None or df.empty or df.isna().all().all():
            return None
        if len(df) == 1:
            return df
        
        if 'time' in df.columns:
            df.set_index('time', inplace=True)

        df_filtered = df.between_time("09:29", "16:00")

        if df_filtered.empty or df_filtered.isna().all().all():
            return None
        
        pl_df = pl.from_pandas(df_filtered.reset_index())

        try:
            seconds_df = pl_df.group_by_dynamic('time', every='1s', label='left').agg([
                pl.count('price').alias('count')
            ])

            max_trades_per_sec = seconds_df.group_by_dynamic('time', every='1m', label='left').agg([
                pl.col('count').max().alias(f'midprice_max_events_per_sec')
            ])
            aggregations = [
                pl.count('price').alias(f'midprice_num_events'),
            ]

            resampled_df = pl_df.group_by_dynamic('time', every='1m', closed='left', label='left').agg(aggregations)

            resampled_df = resampled_df.join(max_trades_per_sec, on='time', how='inner')


            return resampled_df.to_pandas().set_index('time')
        
        except Exception as e:
            print(f"An error occurred: {e}")
            return
        


    def apply_midpoint_aggregations_outside_trading(df, base_date):
        if df is None or df.empty or df.isna().all().all():
            return None
        if len(df) == 1:
            return df
        if 'time' in df.columns:
            df.set_index('time', inplace=True)

        start_time_morning = f"{base_date} 09:30"
        end_time_afternoon = f"{base_date} 16:00"
        df_filtered = df.loc[(df.index < start_time_morning) | (df.index > end_time_afternoon)]

        if df_filtered.empty or df_filtered.isna().all().all():
            return None
        
        pl_df = pl.from_pandas(df_filtered.reset_index())

        try:
            seconds_df = pl_df.group_by_dynamic('time', every='1s', label='left').agg([
                pl.count('price').alias('count')
            ])

            max_trades_per_sec = seconds_df.group_by_dynamic('time', every='30m', label='left').agg([
                pl.col('count').max().alias(f'midprice_max_events_per_sec')
            ])
            aggregations = [
                pl.count('price').alias(f'midprice_num_events'),
            ]

            resampled_df = pl_df.group_by_dynamic('time', every='30m', closed='left', label='left').agg(aggregations)


            resampled_df = resampled_df.join(max_trades_per_sec, on='time', how='inner')


            return resampled_df.to_pandas().set_index('time')
        
        except Exception as e:
            print(f"An error occurred: {e}")
            return


    def process_resample_data(df, interval, base_date=None, outside_trading=False):
        if df is None or df.empty or df.isna().all().all():
            return None
        if len(df) == 1:
            return df

        if 'time' in df.columns:
            df.set_index('time', inplace=True)
        
        if outside_trading:
            start_time_morning = f"{base_date} 09:30"
            end_time_afternoon = f"{base_date} 16:00"
            df_filtered = df.loc[(df.index < start_time_morning) | (df.index > end_time_afternoon)]
        else:
            df_filtered = df.between_time("09:29", "16:00")
        
        if df_filtered.empty or df_filtered.isna().all().all():
            return None

        df_filtered = df_filtered.reset_index()
        pl_df = pl.from_pandas(df_filtered)

        def calculate_vwapr_expr():
            return (pl.col('returns') * pl.col('vol')).sum() / pl.col('vol').sum()

        def calculate_mean_return_expr():
            return pl.col('returns').mean()

        aggregations = [
            calculate_vwapr_expr().alias('returns'),
            pl.col('vol').first().alias('vol')
        ] if 'vol' in df.columns else [
            calculate_mean_return_expr().alias('returns')
        ]

        resampled_df = pl_df.group_by_dynamic('time', every=interval, closed='left').agg(aggregations)

        if resampled_df.is_empty():
            return None

        if not outside_trading:
            resampled_df = resampled_df.filter(
                (pl.col('time') >= pd.to_datetime(f"{base_date} 09:30")) & 
                (pl.col('time') <= pd.to_datetime(f"{base_date} 16:00"))
            )
        if 'vol' in resampled_df.columns:
            resampled_df = resampled_df.drop('vol')
        return resampled_df


    # Processing Trades
    start_process_trades_time = time.time()
    trade_dataframes_to_process = {
        "trades": trades,
        "Buys_trades": Buys_trades,
        "Sells_trades": Sells_trades,
        "Retail_trades": Retail_trades,
        "Oddlot_trades": Oddlot_trades,
    }

    for name, df in trade_dataframes_to_process.items():
        if df is None or df.empty or df.isna().all().all():
            continue
         
        if 'time' in df.columns:
            df.set_index('time', inplace=True)
      
        df_filtered = df.between_time("09:29", "16:00")
        start_time_morning = f"{args.base_date} 09:30"
        end_time_afternoon = f"{args.base_date} 16:00"
        df_filtered_outside = df.loc[(df.index < start_time_morning) | (df.index > end_time_afternoon)]

        if not df_filtered.empty:
            try:
                print(f"Processing {name} DataFrame")
                agg_df = apply_aggregations(df, name)
                agg_df = agg_df.between_time("09:30", "16:00")
                aggregated_data[name] = reindex_to_full_time(agg_df, args.base_date)                
            except KeyError as e:
                print(f"Error processing {name}: {e}")
                continue

        if not df_filtered_outside.empty:
            try:
                print(f"Processing {name} DataFrame outside trading hours")
                agg_df_outside_trading = apply_aggregations_outside_trading(df, name, args.base_date)
                aggregated_data_outside_trading[name] = reindex_to_full_time(agg_df_outside_trading, args.base_date, outside_trading=True)
            except KeyError as e:
                print(f"Error processing {name}: {e} outside trading hours")
                continue

    end_process_trades_time = time.time()

    #Extra variables for trades
    start_process_ΟΙΒ_trades_time = time.time()
    print(f"Processing OIB statistics")

    # orderflow estimation from Buys and Sells
    oib_shr_s = calculate_voib_shr(Buys_trades, Sells_trades)
    if oib_shr_s is not None:
        oib_shr_df = oib_shr_s.to_frame(name='OIB_SHR').reset_index()
        oib_shr_df.columns = ['time', 'OIB_SHR']
    else:
        oib_shr_df = pd.DataFrame(columns=['time', 'OIB_SHR'])

    #Orderflow Statistics (based on the traded volume)
    aggregated_data["OIB_SHR"] = reindex_to_full_time(apply_voib_shr_aggregations(oib_shr_df), args.base_date)
    
    end_process_ΟΙΒ_trades_time = time.time()

    #Herfindahl Index
    start_process_herfindahl_time = time.time()
    print(f"Processing Herfindahl Index")
    def calculate_hindex(df, name):
        if df is None or df.empty or df.isna().all().all():
            return None
        if 'time' in df.columns:
            df.set_index('time', inplace=True)
        if 'value' not in df.columns:
            df['value'] = df['price'] * df['vol']
        df_filtered = df.between_time('09:30', '16:00')
        if df_filtered.empty or df_filtered.isna().all().all():
            return None
        
        pl_df = pl.from_pandas(df_filtered.reset_index())
        
        resampled = pl_df.group_by_dynamic('time', every='1s').agg([
            pl.col('value').sum().alias('sum_of_values'),
            (pl.col('value')**2).sum().alias('sum_of_squared_values')
        ])
        
        
        minutely_data = resampled.group_by_dynamic('time', every='1m').agg([
            pl.col('sum_of_values').sum(),
            pl.col('sum_of_squared_values').sum()
        ])
        minutely_data = minutely_data.with_columns([
            (minutely_data['sum_of_values']**2).alias('sum_of_values_squared')
        ])
        
        minutely_data = minutely_data.with_columns([
            (minutely_data['sum_of_squared_values'] / minutely_data['sum_of_values_squared']).alias('proportion')
        ])
        
        proportion_column_name = f'proportion_{name}'
        minutely_data = minutely_data.select([
            'time', 'proportion'
        ]).rename({
            'proportion': proportion_column_name
        })
        
        aggregated_data[f"hindex_{name}"] = reindex_to_full_time(minutely_data.to_pandas().set_index('time'), args.base_date)

    for df, name in zip([trades, Buys_trades, Sells_trades, Retail_trades, Oddlot_trades, Ask, Bid], 
                        ["trades", "Buys_trades", "Sells_trades", "Retail_trades", "Oddlot_trades", "Ask", "Bid"]):
        calculate_hindex(df, name)
       
    end_process_herfindahl_time = time.time()

    #Processing Midpoint
    start_process_midpoint_time = time.time()

    if not Midpoint.empty:

        if 'time' in df.columns:
            df.set_index('time', inplace=True)
      
        df_filtered = df.between_time("09:29", "16:00")
        start_time_morning = f"{args.base_date} 09:30"
        end_time_afternoon = f"{args.base_date} 16:00"
        df_filtered_outside = df.loc[(df.index < start_time_morning) | (df.index > end_time_afternoon)]
        
        if not df_filtered.empty:
            try:
                print(f"Processing Midpoint DataFrame")
                midpoint_agg_df = apply_midpoint_aggregations(Midpoint)
                midpoint_agg_df = midpoint_agg_df.between_time("09:30", "16:00")
                aggregated_data["Midpoint"] = reindex_to_full_time(midpoint_agg_df, args.base_date)
            except KeyError as e:
                print(f"Error processing Midpoint: {e}")

        if not df_filtered_outside.empty:
            try:
                print(f"Processing Midpoint DataFrame outside trading hours")
                midpoint_agg_df_outside_trading = apply_midpoint_aggregations_outside_trading(Midpoint, args.base_date)
                aggregated_data_outside_trading["Midpoint"] = reindex_to_full_time(midpoint_agg_df_outside_trading, args.base_date, outside_trading=True)
            except KeyError as e:
                print(f"Error processing Midpoint outside trading hours: {e}")        

    end_process_midpoint_time = time.time()

    #Processing Quotes
    start_process_quotes_time = time.time()
    quote_dataframes_to_process = {
        "Ask": Ask,
        "Bid": Bid,
    }


    for name, df in quote_dataframes_to_process.items():
        if df is None or df.empty or df.isna().all().all():
            continue
            
        if 'time' in df.columns:
            df.set_index('time', inplace=True)
      
        df_filtered = df.between_time("09:29", "16:00")
        start_time_morning = f"{args.base_date} 09:30"
        end_time_afternoon = f"{args.base_date} 16:00"
        df_filtered_outside = df.loc[(df.index < start_time_morning) | (df.index > end_time_afternoon)]

        if not df_filtered.empty:
            try:
                print(f"Processing {name} DataFrame")
                agg_df = apply_quote_aggregations(df, name)
                agg_df = agg_df.between_time("09:30", "16:00")
                aggregated_data[name] = reindex_to_full_time(agg_df,  args.base_date)          
            except KeyError as e:
                print(f"Error processing {name}: {e}")
                continue

        if not df_filtered_outside.empty:
            try:
                print(f"Processing {name} DataFrame outside trading hours")
                agg_df_outside_trading = apply_quote_aggregations_outside_trading(df, name, args.base_date)
                aggregated_data_outside_trading[name] = reindex_to_full_time(agg_df_outside_trading,  args.base_date, outside_trading=True)
            except KeyError as e:
                print(f"Error processing {name}: {e} outside trading hours")
                continue

    end_process_quotes_time = time.time()
    

    #Processing Returns
    print(f"Processing Returns")
    start_process_returns_time = time.time()

    trade_returns_1s = process_resample_data(trade_returns, '1s', args.base_date)
    midprice_returns_1s = process_resample_data(midprice_returns, '1s', args.base_date)
    trade_returns_1s_outside_trading = process_resample_data(trade_returns, '1s', args.base_date, outside_trading=True)
    midprice_returns_1s_outside_trading = process_resample_data(midprice_returns, '1s', args.base_date, outside_trading=True)

    aggregated_data["trade_returns"] = reindex_to_full_time(apply_return_aggregations(trade_returns_1s), args.base_date)
    aggregated_data["midprice_returns"] = reindex_to_full_time(apply_return_aggregations(midprice_returns_1s),  args.base_date)
    aggregated_data_outside_trading["trade_returns"] = reindex_to_full_time(apply_return_aggregations_outside_trading(trade_returns_1s_outside_trading),  args.base_date, outside_trading=True)
    aggregated_data_outside_trading["midprice_returns"] = reindex_to_full_time(apply_return_aggregations_outside_trading(midprice_returns_1s_outside_trading),  args.base_date, outside_trading=True)
    
    end_process_returns_time = time.time()

    #Variance Ratios
    start_process_vr_returns_time = time.time()

    for returns_df in [trade_returns, midprice_returns]:
        if returns_df is midprice_returns:
            returns_df_1s = midprice_returns_1s
        else:
            returns_df_1s = trade_returns_1s

        log_returns_5s = process_resample_data(returns_df, '5s', args.base_date)
        log_returns_15s = process_resample_data(returns_df, '15s', args.base_date)

        ratios = {}
        ratios["1sec"] = apply_ret_variances_aggregations(returns_df_1s)
        ratios["5sec"] = apply_ret_variances_aggregations(log_returns_5s)
        ratios["15sec"] = apply_ret_variances_aggregations(log_returns_15s)

        ratios["1sec"].columns = [col + '_1s' for col in ratios["1sec"].columns]
        ratios["5sec"].columns = [col + '_5s' for col in ratios["5sec"].columns]
        ratios["15sec"].columns = [col + '_15s' for col in ratios["15sec"].columns]

        # Merge the two DataFrames on the time index
        if 'variance_5s' in ratios["5sec"].columns and 'variance_15s' in ratios["15sec"].columns:
            variance_ratio_df = pd.merge(ratios["5sec"], ratios["15sec"], left_index=True, right_index=True)
            variance_ratio_df['variance_ratio'] = np.abs((variance_ratio_df['variance_15s'] / (3 * variance_ratio_df['variance_5s'])) - 1)
            if 'variance_1s' in ratios["1sec"].columns: 
                variance_ratio_df = pd.merge(variance_ratio_df, ratios["1sec"], left_index=True, right_index=True)
                variance_ratio_df['variance_ratio2'] = np.abs((variance_ratio_df['variance_5s'] / (5 * variance_ratio_df['variance_1s'])) - 1)

            if returns_df is trade_returns:
                aggregated_data["trade_returns_variance_ratio1"] = reindex_to_full_time(variance_ratio_df['variance_ratio'],  args.base_date)
                if 'variance_1s' in ratios["1sec"].columns: 
                    aggregated_data["trade_returns_variance_ratio2"] = reindex_to_full_time(variance_ratio_df['variance_ratio2'],  args.base_date) 

            else:
                aggregated_data["midprice_returns_variance_ratio1"] = reindex_to_full_time(variance_ratio_df['variance_ratio'],  args.base_date)
                if 'variance_1s' in ratios["1sec"].columns: 
                    aggregated_data["midprice_returns_variance_ratio2"] = reindex_to_full_time(variance_ratio_df['variance_ratio2'],  args.base_date) 
        else:
            print(f"Missing required columns for variance ratio calculation")
    
    end_process_vr_returns_time = time.time()

    

    #End calculation time
    main_end_time = time.time()
    #    print("Structure of aggregated_data:")
    #    for name, df in aggregated_data.items():
    #        print(f"DataFrame name: {name}")
    #        print(df.head())  # Print the first few rows of each DataFrame
    #        print(df.info())  # Print the summary of the DataFrame
    #        for column in df.columns:
    #            print(f"Column {column} has type: {df[column].dtype}")
    #       print("-" * 50)

    write_start_time = time.time()
    

    # Function to reindex DataFrames to the full time index
    def merge_dataframes(df1, df2):
        return pd.merge(df1, df2, left_index=True, right_index=True, how='outer')
    
    consolidated_df = pd.DataFrame()
    for name, df in aggregated_data.items():
        if df is not None and not df.isna().all().all():
            consolidated_df = merge_dataframes(consolidated_df, df) if not consolidated_df.empty else df

    consolidated_df_outside_trading = pd.DataFrame()
    for name, df in aggregated_data_outside_trading.items():
        if df is not None and not df.isna().all().all():
            consolidated_df_outside_trading = merge_dataframes(consolidated_df_outside_trading, df) if not consolidated_df_outside_trading.empty else df

    consolidated_df.reset_index(inplace=True)
    consolidated_df.rename(columns={'index': 'time'}, inplace=True)

    consolidated_df_outside_trading.reset_index(inplace=True)
    consolidated_df_outside_trading.rename(columns={'index': 'time'}, inplace=True)

    # Print consolidated_df to ensure it's a DataFrame
    #print(consolidated_df)
    #print(consolidated_df_outside_trading)


    def process_and_save_df(df, hdf5_variable_path, output_file_path, stock_name, day, month, year, time_range_name):
        if not df.empty:
            # Convert object columns to string
            for col in df.columns:
                if df[col].dtype == "object":
                    df[col] = df[col].astype(str)
            
            # Convert datetime columns to formatted string and collect their names
            datetime_columns = []
            for col in df.columns:
                if df[col].dtype == "datetime64[ns]":
                    df[col] = df[col].dt.strftime("%Y-%m-%d %H:%M:%S.%f")
                    datetime_columns.append(col)
            
            # Write datetime column names to a file
            try:
                with open(output_file_path, "w") as f:
                    for column in datetime_columns:
                        f.write(f"{column}\n")
                print("Datetime column names have been successfully written to the file.")
            except IOError as e:
                print(f"An error occurred while writing to the file: {e}")
            
            # Save data to HDF5 file
            print(f"Saving data to HDF5 file: {hdf5_variable_path}")
            with pd.HDFStore(hdf5_variable_path, mode="a", complevel=9, complib="zlib") as store:
                hdf5_key = f"/{stock_name}/day{day}/{time_range_name}"
                store.append(hdf5_key, df, format="table", data_columns=True, index=False)
                print(f"Data successfully saved to HDF5 key: {hdf5_key}")
        else:
            print("No DataFrames to merge. Skipping HDF5 save step.")
            empty_bars_file_path = "/home/taq/taq_variables/empty_time_bars.txt"
            message = f"{stock_name} has empty time bars for {day}/{month}/{year}."
            
            try:
                with open(empty_bars_file_path, "w") as f:
                    f.write(message)
                print(f"Message written to {empty_bars_file_path}")
            except IOError as e:
                print(f"An error occurred while writing to the file: {e}")

    if consolidated_df is not None and not consolidated_df.empty:
        output_file_path_consolidated = "/home/taq/taq_variables/datetime_columns_consolidated.txt"
        process_and_save_df(consolidated_df, hdf5_variable_path, output_file_path_consolidated, args.stock_name, args.day, args.month, args.year, "time_bars")
    else:
        print("Consolidated DataFrame is empty or None. Skipping save.")

    if consolidated_df_outside_trading is not None and not consolidated_df_outside_trading.empty:
        output_file_path_outside = "/home/taq/taq_variables/datetime_columns_outside.txt"
        process_and_save_df(consolidated_df_outside_trading, hdf5_variable_path, output_file_path_outside, args.stock_name, args.day, args.month, args.year, "outside_trading_time_bars")
    else:
        print("Consolidated DataFrame outside trading is empty or None. Skipping save.")


    write_end_time = time.time()

    with open("variables_time.txt", "a") as f:
        f.write(f"Stock: {args.stock_name}\n")
        f.write(f"Day: {args.day}\n")
        f.write(f"Only the calculation runtime: {main_end_time - main_start_time} seconds\n")
        f.write(f"Only the trade processing: {end_process_trades_time - start_process_trades_time} seconds\n")
        f.write(f"OIB processing: {end_process_ΟΙΒ_trades_time - start_process_ΟΙΒ_trades_time} seconds\n")
        f.write(f"Herfindahl Index processing: {end_process_herfindahl_time- start_process_herfindahl_time} seconds\n")
        f.write(f"Only the quote processing: {end_process_quotes_time - start_process_quotes_time} seconds\n")
        f.write(f"Only the midpoint processing: {end_process_midpoint_time - start_process_midpoint_time} seconds\n")
        f.write(f"Only the return processing: {end_process_returns_time - start_process_returns_time} seconds\n")

        f.write(f"Only the variance ratios processing: {end_process_vr_returns_time - start_process_vr_returns_time} seconds\n")
        f.write(f"Write runtime: {write_end_time - write_start_time} seconds\n")

if __name__ == "__main__":
    # Profile the main function
    pr = cProfile.Profile()
    pr.enable()
    main()
    pr.disable()

    # Save profiling results
    profiling_file = "profiling_total_main_f.txt"
    with open(profiling_file, "a") as f:
        f.write(f"\nStock: {args.stock_name}\n")
        ps = pstats.Stats(pr, stream=f)
        ps.strip_dirs().sort_stats(pstats.SortKey.CUMULATIVE).print_stats()
