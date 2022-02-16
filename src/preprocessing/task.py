import argparse
import json
import logging
import os
import sys
import time

from dask.distributed import Client
from dask_cuda import LocalCUDACluster
import fsspec
from google.cloud import bigquery
import nvtabular as nvt
from nvtabular.io.shuffle import Shuffle
from nvtabular.ops import Categorify
from nvtabular.ops import Clip
from nvtabular.ops import FillMissing
from nvtabular.ops import Normalize
from nvtabular.utils import device_mem_size

import numpy as np
from typing import Dict, List, Union

def create_csv_dataset(
    data_paths,
    sep,
    recursive,
    col_dtypes,
    frac_size,
    client
):
  """Create nvt.Dataset definition for CSV files."""
  fs_spec = fsspec.filesystem('gs')
  rec_symbol = '**' if recursive else '*'

  valid_paths = []
  for path in data_paths:
    try:
      if fs_spec.isfile(path):
        valid_paths.append(path)
      else:
        path = os.path.join(path, rec_symbol)
        for i in fs_spec.glob(path):
          if fs_spec.isfile(i):
            valid_paths.append(f'gs://{i}')
    except FileNotFoundError as fnf_expt:
      print(fnf_expt)
      print('Incorrect path: {path}.')
    except OSError as os_err:
      print(os_err)
      print('Verify access to the bucket.')

    return nvt.Dataset(
        path_or_source=valid_paths,
        engine='csv',
        names=list(col_dtypes.keys()),
        sep=sep,
        dtypes=col_dtypes,
        part_mem_fraction=frac_size,
        client=client,
        assume_missing=True
    )


def convert_csv_to_parquet(
    output_path,
    dataset,
    output_files,
    shuffle=None
):
  """Convert CSV file to parquet and write to GCS."""
  if shuffle == 'None':
    shuffle = None
  else:
    try:
      shuffle = getattr(Shuffle, shuffle)
    except:
      print('Shuffle method not available. Using default.')
      shuffle = None

  dataset.to_parquet(
      output_path,
      shuffle=shuffle,
      output_files=output_files
  )


def create_criteo_nvt_workflow(client):
  """Create a nvt.Workflow definition with transformation all the steps."""
  # Columns definition
  cont_names = ['I' + str(x) for x in range(1, 14)]
  cat_names = ['C' + str(x) for x in range(1, 27)]

  # Transformation pipeline
  num_buckets = 10000000
  categorify_op = Categorify(max_size=num_buckets)
  cat_features = cat_names >> categorify_op
  cont_features = cont_names >> FillMissing() >> Clip(
      min_value=0) >> Normalize()
  features = cat_features + cont_features + ['label']

  # Create and save workflow
  return nvt.Workflow(features, client)


def create_cluster(
    n_workers,
    device_limit_frac,
    device_pool_frac,
    memory_limit
):
  """Create a Dask cluster to apply the transformations steps to the Dataset."""
  device_size = device_mem_size()
  device_limit = int(device_limit_frac * device_size)
  device_pool_size = int(device_pool_frac * device_size)
  rmm_pool_size = (device_pool_size // 256) * 256

  cluster = LocalCUDACluster(
      n_workers=n_workers,
      device_memory_limit=device_limit,
      rmm_pool_size=rmm_pool_size,
      memory_limit=memory_limit
  )

  return Client(cluster)


def create_parquet_dataset(
    client,
    data_path,
    frac_size
):
  """Create a nvt.Dataset definition for the parquet files."""
  fs = fsspec.filesystem('gs')
  file_list = fs.glob(
      os.path.join(data_path, '*.parquet')
  )

  if not file_list:
    raise FileNotFoundError('Parquet file(s) not found')

  file_list = [os.path.join('gs://', i) for i in file_list]

  return nvt.Dataset(
      file_list,
      engine='parquet',
      part_mem_fraction=frac_size,
      client=client
  )


def analyze_dataset(
    workflow,
    dataset,
):
  """Calculate statistics for a given workflow."""
  workflow.fit(dataset)
  return workflow


def transform_dataset(
    dataset,
    workflow
):
  """Apply the transformations to the dataset."""
  workflow.transform(dataset)
  return dataset


def load_workflow(
    workflow_path,
    client,
):
  """Load a workflow definition from a path."""
  return nvt.Workflow.load(workflow_path, client)


def save_workflow(
    workflow,
    output_path
):
  """Save workflow to a path."""
  workflow.save(output_path)


def save_dataset(
    dataset,
    output_path,
    shuffle=None
):
  """Save dataset to parquet files to path."""
  if shuffle == 'None':
    shuffle = None
  else:
    try:
      shuffle = getattr(Shuffle, shuffle)
    except:
      print('Shuffle method not available. Using default.')
      shuffle = None

  dataset.to_parquet(
      output_path=output_path,
      shuffle=shuffle
  )


def extract_table_from_bq(
    client,
    output_dir,
    dataset_ref,
    table_id,
    location='us'
):
  """Create job to extract parquet files from BQ tables."""
  extract_job_config = bigquery.ExtractJobConfig()
  extract_job_config.destination_format = 'PARQUET'

  bq_glob_path = os.path.join(output_dir, 'criteo-*.parquet')
  table_ref = dataset_ref.table(table_id)

  extract_job = client.extract_table(
      table_ref,
      bq_glob_path,
      location=location,
      job_config=extract_job_config
  )
  extract_job.result()


def get_criteo_col_dtypes() -> Dict[str, Union[str, np.int32]]:
  """Returns a dict mapping column names to numpy dtype."""
  # Specify column dtypes. Note that "hex" means that
  # the values will be hexadecimal strings that should
  # be converted to int32
  col_dtypes = {}

  col_dtypes["label"] = np.int32
  for x in ["I" + str(i) for i in range(1, 14)]:
    col_dtypes[x] = np.int32
  for x in ["C" + str(i) for i in range(1, 27)]:
    col_dtypes[x] = "hex"

  return col_dtypes


def main_convert(args):
  logging.info('Creating cluster')
  client = create_cluster(
    args.n_workers,
    args.device_limit_frac,
    args.device_pool_frac,
    args.memory_limit
  )

  logging.info('Creating CSV dataset')
  dataset = create_csv_dataset(
    args.csv_data_path, 
    args.sep,
    False, 
    get_criteo_col_dtypes(), 
    args.frac_size, 
    client
  )

  logging.info('Converting CSV to Parquet')
  convert_csv_to_parquet(
    args.output_path,
    dataset,
    args.output_files
  )


def main_analyse(args):
  logging.info('Creating cluster')
  client = create_cluster(
    args.n_workers,
    args.device_limit_frac,
    args.device_pool_frac,
    args.memory_limit
  )

  logging.info('Creating Parquet dataset')
  dataset = create_parquet_dataset(
    client=client, 
    data_path=args.parquet_data_path,
    frac_size=args.frac_size
  )

  logging.info('Creating Workflow')
  # Create Workflow
  criteo_workflow = create_criteo_nvt_workflow(client)

  logging.info('Analyzing dataset')
  criteo_workflow = analyze_dataset(criteo_workflow, dataset)

  logging.info('Saving Workflow')
  save_workflow(criteo_workflow, args.output_path)


def main_transform(args):
  logging.info('Creating cluster')
  client = create_cluster(
    args.n_workers,
    args.device_limit_frac,
    args.device_pool_frac,
    args.memory_limit
  )

  logging.info('Creating Parquet dataset')
  dataset = create_parquet_dataset(
    client=client, 
    data_path=args.parquet_data_path, 
    frac_size=args.frac_size
  )

  logging.info('Loading Workflow')
  criteo_workflow = load_workflow(
    args.workflow_path,
    client
  )

  logging.info('Transforming Dataset')
  transformed_dataset = transform_dataset(
    dataset,
    criteo_workflow
  )

  logging.info('Saving transformed dataset')
  save_dataset(
    transformed_dataset,
    output_path=args.output_path
  )


def parse_args():
  """Parses command line arguments."""

  parser = argparse.ArgumentParser()
  parser.add_argument('--task',
                      type=str,
                      required=False)
  parser.add_argument('--csv_data_path',
                      required=False,
                      nargs='+')
  parser.add_argument('--parquet_data_path',
                      type=str,
                      required=False)
  parser.add_argument('--output_path',
                      type=str,
                      required=False)
  parser.add_argument('--output_files',
                      type=int,
                      required=False)
  parser.add_argument('--workflow_path',
                      type=str,
                      required=False)
  parser.add_argument('--n_workers',
                      type=int,
                      required=False)
  parser.add_argument('--sep',
                      type=str,
                      required=False)
  parser.add_argument('--frac_size',
                      type=float,
                      required=False,
                      default=0.10)
  parser.add_argument('--memory_limit',
                      type=int,
                      required=False,
                      default=100_000_000_000)
  parser.add_argument('--device_limit_frac',
                      type=float,
                      required=False,
                      default=0.60)
  parser.add_argument('--device_pool_frac',
                      type=float,
                      required=False,
                      default=0.90)

  return parser.parse_args()


if __name__ == '__main__':
  logging.basicConfig(format='%(asctime)s - %(message)s',
                      level=logging.INFO, 
                      datefmt='%d-%m-%y %H:%M:%S',
                      stream=sys.stdout)

  parsed_args = parse_args()

  start_time = time.time()
  logging.info('Timing task')

  if parsed_args.task == 'convert':
      main_convert(parsed_args)
  elif parsed_args.task == 'analyse':
      main_analyse(parsed_args)
  elif parsed_args.task == 'transform':
      main_transform(parsed_args)

  end_time = time.time()
  elapsed_time = end_time - start_time
  logging.info('Task completed. Elapsed time: %s', elapsed_time)