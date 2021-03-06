# Copyright 2015 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Run YCSB benchmark against Google Cloud Datastore.

Before running this benchmark, you have to download your P12
service account private key file to local machine, and pass the path
via 'google_datastore_keyfile' parameters to PKB.

Service Account email associated with the key file is also needed to
pass to PKB.

By default, this benchmark provision 1 single-CPU VM and spawn 1 thread
to test Datastore.
"""

import concurrent.futures
import logging
import time

from perfkitbenchmarker import configs
from perfkitbenchmarker import errors
from perfkitbenchmarker import flags
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.linux_packages import ycsb

from google.cloud import datastore
from google.oauth2 import service_account


BENCHMARK_NAME = 'cloud_datastore_ycsb'
BENCHMARK_CONFIG = """
cloud_datastore_ycsb:
  description: >
      Run YCSB agains Google Cloud Datastore.
      Configure the number of VMs via --num-vms.
  vm_groups:
    default:
      vm_spec: *default_single_core
      vm_count: 1"""

_CLEANUP_THREAD_POOL_WORKERS = 20
_CLEANUP_KIND_READ_BATCH_SIZE = 20000
_CLEANUP_KIND_DELETE_BATCH_SIZE = 500
# the name of the database entity created when running datastore YCSB
# https://github.com/brianfrankcooper/YCSB/tree/master/googledatastore
_YCSB_COLLECTIONS = ['usertable']

FLAGS = flags.FLAGS
flags.DEFINE_string('google_datastore_keyfile', None,
                    'The path to Google API P12 private key file')
flags.DEFINE_string(
    'private_keyfile', '/tmp/key.p12',
    'The path where the private key file is copied to on a VM.')
flags.DEFINE_string(
    'google_datastore_serviceAccount', None,
    'The service account email associated with'
    'datastore private key file')
flags.DEFINE_string('google_datastore_datasetId', None,
                    'The project ID that has Cloud Datastore service')
flags.DEFINE_string('google_datastore_debug', 'false',
                    'The logging level when running YCSB')
# the JSON keyfile is needed to validate credentials in the Cleanup phase
flags.DEFINE_string('google_datastore_deletion_keyfile', None,
                    'The path to Google API JSON private key file')


class _DeletionTask(object):
  """Represents a cleanup deletion task.

  Attributes:
    kind: Datastore kind to be deleted.
    task_id: Task id
    query_iter: Query iterator used to read entities.
    entities: Entities to be deleted.
    entity_read_count: No of entities read.
    entity_deletion_count: No of entities deleted.
    deletion_error: Set to true if deletion fails with an error.
  """

  def __init__(self, kind, task_id):
    self.kind = kind
    self.task_id = task_id
    self.query_iter = None
    self.entities = None
    self.entity_read_count = 0
    self.entity_deletion_count = 0
    self.deletion_error = False

  def ReadEntities(self, query, start_cursor, current_entity_read_count):
    """Read next batch of entities in a datastore database using a cursor.

    Args:
      query: Cloud Datastore query to get entities.
      start_cursor: Start cursor for the query.
      current_entity_read_count: No of entities read uptil now.

    Returns:
      The updated total read count of entities.

    Raises:
      ValueError: In case of delete failures.
    """
    self.query_iter = query.fetch(
        start_cursor, limit=_CLEANUP_KIND_READ_BATCH_SIZE)
    self.entities = list(self.query_iter.pages)
    self.entity_read_count = len(self.entities)
    current_entity_read_count += self.entity_read_count
    logging.info('Task %d - Read %d entities for %s kind , Read %d in total.',
                 self.task_id, self.entity_read_count, self.kind,
                 current_entity_read_count)
    return current_entity_read_count

  def DeleteEntities(self, delete_client):
    """Deletes entities in a datastore database in batches.

    Args:
      delete_client: Cloud Datastore client to delete entities.

    Raises:
      ValueError: In case of delete failures.
    """
    if self.entity_read_count >= 1:
      logging.info('Task %d - Deleting %d entities for %s', self.task_id,
                   self.entity_read_count, self.kind)
      try:
        while self.entities:
          chunk = self.entities[:_CLEANUP_KIND_DELETE_BATCH_SIZE]
          self.entities = self.entities[_CLEANUP_KIND_DELETE_BATCH_SIZE:]
          delete_client.delete_multi(entity.key for entity in chunk)
          self.entity_deletion_count += _CLEANUP_KIND_DELETE_BATCH_SIZE

        logging.info('Task %d - Finished %d deletes for %s', self.task_id,
                     self.entity_deletion_count, self.kind)
      except ValueError as error:
        logging.error('Task %d - Delete entities for %s failed due to %s',
                      self.task_id, self.kind, error)
        self.deletion_error = True
        raise error
    else:
      logging.info('Task %d for %s - No entities to delete', self.task_id,
                   self.kind)


def GetConfig(user_config):
  config = configs.LoadConfig(BENCHMARK_CONFIG, user_config, BENCHMARK_NAME)
  if FLAGS['ycsb_client_vms'].present:
    config['vm_groups']['default']['vm_count'] = FLAGS.ycsb_client_vms
  return config


def CheckPrerequisites(_):
  """Verifies that the required resources are present.

  Raises:
    perfkitbenchmarker.data.ResourceNotFound: On missing resource.
  """
  # Before YCSB Cloud Datastore supports Application Default Credential,
  # we should always make sure valid credential flags are set.
  if not FLAGS.google_datastore_keyfile:
    raise ValueError('"google_datastore_keyfile" must be set')
  if not FLAGS.google_datastore_serviceAccount:
    raise ValueError('"google_datastore_serviceAccount" must be set')
  if not FLAGS.google_datastore_datasetId:
    raise ValueError('"google_datastore_datasetId" must be set ')


def GetDatastoreDeleteCredentials():
  """Returns credentials to datastore db."""
  if FLAGS.google_datastore_deletion_keyfile.startswith('gs://'):
    # Copy private keyfile to local disk
    cp_cmd = [
        'gsutil', 'cp', FLAGS.google_datastore_deletion_keyfile,
        FLAGS.private_keyfile
    ]
    vm_util.IssueCommand(cp_cmd)
    credentials_path = FLAGS.private_keyfile
  else:
    credentials_path = FLAGS.google_datastore_deletion_keyfile

  credentials = service_account.Credentials.from_service_account_file(
      credentials_path,
      scopes=datastore.client.Client.SCOPE,
  )

  return credentials


def Prepare(benchmark_spec):
  """Prepare the virtual machines to run cloud datastore.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
        required to run the benchmark.
  """
  benchmark_spec.always_call_cleanup = True

  # Check that the database is empty before running
  if FLAGS.google_datastore_deletion_keyfile:
    dataset_id = FLAGS.google_datastore_datasetId
    credentials = GetDatastoreDeleteCredentials()

    client = datastore.Client(project=dataset_id, credentials=credentials)

    for kind in _YCSB_COLLECTIONS:
      if list(client.query(kind=kind).fetch(limit=1)):
        raise errors.Benchmarks.PrepareException(
            'Database is non-empty. Stopping test.')

  else:
    logging.warning('Test could be executed on a non-empty database.')

  vms = benchmark_spec.vms

  # Install required packages and copy credential files
  vm_util.RunThreaded(_Install, vms)

  # Restore YCSB_TAR_URL
  benchmark_spec.executor = ycsb.YCSBExecutor('googledatastore')


def Run(benchmark_spec):
  """Spawn YCSB and gather the results.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
        required to run the benchmark.

  Returns:
    A list of sample.Sample instances.
  """
  vms = benchmark_spec.vms
  run_kwargs = {
      'googledatastore.datasetId': FLAGS.google_datastore_datasetId,
      'googledatastore.privateKeyFile': FLAGS.private_keyfile,
      'googledatastore.serviceAccountEmail':
          FLAGS.google_datastore_serviceAccount,
      'googledatastore.debug': FLAGS.google_datastore_debug,
  }
  load_kwargs = run_kwargs.copy()
  if FLAGS['ycsb_preload_threads'].present:
    load_kwargs['threads'] = FLAGS['ycsb_preload_threads']
  samples = list(benchmark_spec.executor.LoadAndRun(
      vms, load_kwargs=load_kwargs, run_kwargs=run_kwargs))
  return samples


def Cleanup(_):
  """Deletes all entries in a datastore database."""
  if FLAGS.google_datastore_deletion_keyfile:
    dataset_id = FLAGS.google_datastore_datasetId
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=_CLEANUP_THREAD_POOL_WORKERS)

    logging.info('Attempting to delete all data in %s', dataset_id)

    credentials = GetDatastoreDeleteCredentials()

    futures = []
    for kind in _YCSB_COLLECTIONS:
      futures.append(
          executor.submit(
              _ReadAndDeleteAllEntities(executor, dataset_id, credentials,
                                        kind)))

    concurrent.futures.wait(
        futures, timeout=None, return_when=concurrent.futures.ALL_COMPLETED)
    logging.info('Deleted all data for %s', dataset_id)

  else:
    logging.warning('Manually delete all the entries via GCP portal.')


def _ReadAndDeleteAllEntities(executor, dataset_id, credentials, kind):
  """Reads and deletes all kind entries in a datastore database.

  Args:
    executor: ThreadPool to submit delete tasks.
    dataset_id: Cloud Datastore client dataset id.
    credentials: Cloud Datastore client credentials.
    kind: Kind for which entities will be deleted.

  Raises:
    ValueError: In case of delete failures.
  """
  futures = []
  total_entity_count = 0
  task_id = 1

  query = _CreateClient(dataset_id, credentials).query(kind=kind)
  query.keys_only()

  # We use a cursor to fetch entities in larger read batches and submit delete
  # tasks to delete them in smaller delete batches (500 at a time) due to
  # datastore single operation restriction.
  deletion_task = _DeletionTask(kind, task_id)
  total_entity_count = deletion_task.ReadEntities(query, None,
                                                  total_entity_count)
  futures.append(
      executor.submit(
          deletion_task.DeleteEntities(_CreateClient(dataset_id, credentials))))

  while deletion_task.query_iter.next_page_token is not None:
    task_id += 1
    deletion_task = _DeletionTask(kind, task_id)
    total_entity_count = deletion_task.ReadEntities(
        query, deletion_task.query_iter.next_page_token, total_entity_count)
    futures.append(
        executor.submit(
            deletion_task.DeleteEntities(
                _CreateClient(dataset_id, credentials))))
    if task_id % 10 == 0:
      # Sleep for 10 secs before fetching next batch.
      time.sleep(10)

  concurrent.futures.wait(
      futures, timeout=None, return_when=concurrent.futures.ALL_COMPLETED)
  logging.info('Deleted all data for %s - %s - %d', dataset_id, kind,
               total_entity_count)


def _CreateClient(dataset_id, credentials):
  """Creates a datastore client for the dataset using the credentials.

  Args:
    dataset_id: Cloud Datastore client dataset id.
    credentials: Cloud Datastore client credentials.

  Returns:
    Datastore client.
  """
  return datastore.Client(project=dataset_id, credentials=credentials)


def _Install(vm):
  """Installs YCSB benchmark & copies datastore keyfile to client vm."""
  vm.Install('ycsb')

  # Copy private key file to VM
  if FLAGS.google_datastore_keyfile.startswith('gs://'):
    vm.Install('google_cloud_sdk')
    vm.RemoteCommand('{cmd} {datastore_keyfile} {private_keyfile}'.format(
        cmd='gsutil cp',
        datastore_keyfile=FLAGS.google_datastore_keyfile,
        private_keyfile=FLAGS.private_keyfile))
  else:
    vm.RemoteCopy(FLAGS.google_datastore_keyfile, FLAGS.private_keyfile)
