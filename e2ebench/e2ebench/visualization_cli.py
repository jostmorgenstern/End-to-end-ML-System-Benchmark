"""e2ebench CLI for visualizing metrics

The visualization frontend of e2ebench.
Given a database file as created by an e2ebench.Benchmark object,
this tool gives a selection of available metrics and helps users choose a subset to visualize.
Users can select metrics in two ways:
    1. by using command line arguments (see e2ebench-cli -h)
    2. If no arguments are provided, e2ebench-cli will prompt the user for arguments.
All metrics of the same measurement type are then visualized in a single diagram.
"""

import argparse
from itertools import chain
import pickle
import os
import sys

import pandas as pd
import matplotlib.pyplot as plt
from PyInquirer import prompt, Separator
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine, asc

from e2ebench.datamodel import Measurement, BenchmarkMetadata
from e2ebench.visualization import type_to_visualizer_class_mapper

def get_args():
    file_help = "Sqlite Database file as created by an e2ebench.Benchmark object"
    module_help = "Visualization CLI for End to End ML System Benchmark"
    uuid_help = "UUUIDs of the runs to visualize. Each uuid corresponds to one pipeline run. " + \
                "For humans uuids are usually tedious to handle. Leave this parameter out to be shown a list of available uuids to choose from."
    type_help = "Measurement types of the metrics to visualize. Each metric that e2ebench supports has a descriptive type. " + \
                "Like the uuids, this parameter is optional and can be choosen by prompt."
    description_help = "Descriptions are supplied by users during the implementation stage of a pipeline. " + \
                       "They help giving descriptive information about captured metrics. " + \
                       "This parameter is optional and can be choosen via prompt later."
    plotting_backend_help = "Plotting backend used for visualization. The default is matplotlib."


    parser = argparse.ArgumentParser(description=module_help)
    parser.add_argument("file", help=file_help)
    parser.add_argument("-u", "--uuids", nargs="+", help=uuid_help, required=False)
    parser.add_argument("-t", "--types", nargs="+", help=type_help, required=False)
    parser.add_argument("-d", "--descriptions", nargs="+", help=description_help, required=False)
    parser.add_argument("-p", "--plotting-backend", choices=["matplotlib", "plotly"], default="matplotlib", help=plotting_backend_help)
    
    return parser.parse_args()

def create_dfs(session):
    meas_query = session.query(Measurement.id,
                               Measurement.benchmark_uuid,
                               Measurement.measurement_type,
                               Measurement.description)
    meta_query = session.query(BenchmarkMetadata.uuid,
                               BenchmarkMetadata.description,
                               BenchmarkMetadata.start_time)
    meas_df = pd.DataFrame(meas_query.all(), columns=['id', 'uuid', 'measurement_type', 'measurement_desc'])
    meta_df = pd.DataFrame(meta_query.all(), columns=['uuid', 'meta_desc', 'meta_start_time'])

    return meas_df, meta_df

def filter_by_args(meas_df, meta_df, args):
    if args.uuids:
        meas_df = meas_df[meas_df['uuid'].isin([args.uuids])]
        meta_df = meta_df[meta_df['uuid'].isin([args.uuids])]
    if args.types:
        meas_df = meas_df[meas_df['measurement_type'].isin([args.types])]
    if args.descriptions:
        meas_df = meas_df[meas_df['measurement_desc'].isin([args.descriptions])]

    if meas_df.empty:
        raise Exception("There are no database entries with the given uuids, types and descriptions.")

    return meas_df, meta_df

def prompt_for_uuid(meas_df, meta_df):
    prompt_choices = [
            {'name' : 
                f"{db_entry['uuid']}" +
                f", {str(db_entry['meta_start_time'])}" +
                f"{', description: ' + db_entry['meta_desc'] if db_entry['meta_desc'] else ''}",
             'value': db_entry['uuid']}
            for _, db_entry in meta_df.iterrows()
    ]
    uuid_prompt = {
        'type' : 'checkbox',
        'name' : 'uuids',
        'message' : 'Please select one or more uuids.',
        'choices' : prompt_choices
    }
    chosen_uuids = prompt(uuid_prompt)['uuids']
    meas_df = meas_df[meas_df['uuid'].isin(chosen_uuids)]
    meta_df = meta_df[meta_df['uuid'].isin(chosen_uuids)]

    return meas_df, meta_df

def prompt_for_types(meas_df):
    prompt_choices = []
    for uuid, uuid_group in meas_df.groupby('uuid'):
        prompt_choices.append(Separator(f"Available types for uuid {uuid}"))
        for meas_type, meas_type_group in uuid_group.groupby('measurement_type'):
            prompt_choices.append(
                {'name' : meas_type,
                 'value' : list(meas_type_group['id'])}
            )
    type_prompt = {
        'type' : 'checkbox',
        'name' : 'measurement_types',
        'message' : 'Please select measurement types corresponding to uuids.',
        'choices' : prompt_choices
    }
    prompt_res = prompt(type_prompt)['measurement_types']
    idx = list(chain(*prompt_res))
    meas_df = meas_df[meas_df['id'].isin(idx)]
    
    return meas_df

def prompt_for_description(meas_df):
    prompt_choices = []
    for (uuid, meas_type), u_t_group in meas_df.groupby(['uuid', 'measurement_type']):
        prompt_choices.append(Separator(f"Available descriptions for uuid {uuid} and type {meas_type}"))
        for desc, desc_group in u_t_group.groupby('measurement_desc'):
            prompt_choices.append(
                {'name' : desc,
                 'value' : list(desc_group['id'])}
            )
    desc_prompt = {
        'type' : 'checkbox',
        'name' : 'descriptions',
        'message' : 'Please select descriptions corresponding to uuids and types.',
        'choices' : prompt_choices
    }
    prompt_res = prompt(desc_prompt)['descriptions']
    idx = list(chain(*prompt_res))
    meas_df = meas_df[meas_df['id'].isin(idx)]
    
    return meas_df

def join_remaining_columns(meas_df, meta_df, session):
    serialized_query = session.query(Measurement.id, 
                                     Measurement.datetime,
                                     Measurement.value,
                                     Measurement.unit).filter(Measurement.id.in_(meas_df['id']))
    serialized_df = pd.DataFrame(serialized_query.all(), 
                                 columns=['id', 'measurement_time', 'bytes', 'measurement_unit'])
    serialized_df['measurement_data'] = serialized_df['bytes'].map(pickle.loads)
    serialized_df.drop(columns=['bytes'], inplace=True)
    meas_df = meas_df.merge(serialized_df, on='id')
    meas_df = meas_df.merge(meta_df, on='uuid')

    return meas_df


def main():
    args = get_args()

    engine = create_engine(f'sqlite+pysqlite:///{args.file}')
    Session = sessionmaker(bind=engine)
    session = Session()

    meas_df, meta_df = create_dfs(session)
    meas_df, meta_df = filter_by_args(meas_df, meta_df, args)
    
    if args.uuids is None:
        meas_df, meta_df = prompt_for_uuid(meas_df, meta_df)
    if args.types is None:
        meas_df = prompt_for_types(meas_df)
    if args.descriptions is None:
        meas_df = prompt_for_description(meas_df)
    
    df = join_remaining_columns(meas_df, meta_df, session)
    
    figs = []

    for meas_type, type_group_df in df.groupby('measurement_type'):
        #type_group_df.index = range(len(type_group_df))
        VisualizerClass = type_to_visualizer_class_mapper[meas_type]
        visualizer = VisualizerClass(type_group_df, args.plotting_backend)
        figs.append(visualizer.plot())

        # Needs to be flattened since visualizers can either return a figure or a list of figures
    figs = list(chain(*figs))

    if args.plotting_backend == 'matplotlib':
        plt.show()
    if args.plotting_backend == 'plotly':
        for fig in figs:
            fig.show()


    session.close()


if __name__ == "__main__":
    main()
