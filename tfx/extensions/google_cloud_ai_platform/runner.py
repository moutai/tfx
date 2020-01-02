# Lint as: python2, python3
# Copyright 2019 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Helper class to start TFX training jobs on AI Platform."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import datetime
import json
import sys
import time
from typing import Any, Dict, List, Optional, Text

import absl
from googleapiclient import discovery
from googleapiclient import errors
import tensorflow as tf

from tfx import types
from tfx import version
from tfx.types import artifact_utils
from tfx.utils import monitoring_utils

_POLLING_INTERVAL_IN_SECONDS = 30

# TODO(b/139934802) Ensure mirroring of released TFX containers in Docker Hub
# and gcr.io/tfx-oss-public/ registries.
_TFX_IMAGE = 'gcr.io/tfx-oss-public/tfx:%s' % (version.__version__)

# Compatibility overrides: this is usually result of lags for CAIP releases
# after tensorflow.
_TF_COMPATIBILITY_OVERRIDE = {
    # TODO(b/142654646): Support TF 1.15 in CAIP prediction service and drop
    # this entry. This is generally considered safe since we are using same
    # major version of TF.
    '1.15': '1.14',
}


def _get_tf_runtime_version() -> Text:
  """Returns the tensorflow runtime version used in Cloud AI Platform.

  This is only used for prediction service.

  Returns: same major.minor version of installed tensorflow, except when
    overriden by _TF_COMPATIBILITY_OVERRIDE.
  """
  # runtimeVersion should be same as <major>.<minor> of currently
  # installed tensorflow version, with certain compatibility hacks since
  # some versions of TensorFlow are not yet supported by CAIP pusher.
  tf_version = '.'.join(tf.__version__.split('.')[0:2])
  if tf_version.startswith('2'):
    absl.logging.warn(
        'tensorflow 2.x may not be supported on CAIP predction service yet, '
        'please check https://cloud.google.com/ml-engine/docs/runtime-version-list to ensure.'
    )
  return _TF_COMPATIBILITY_OVERRIDE.get(tf_version, tf_version)


def _get_caip_python_version() -> Text:
  """Returns supported python version on Cloud AI Platform.

  See
  https://cloud.google.com/ml-engine/docs/tensorflow/versioning#set-python-version-training

  Returns:
    '2.7' for PY2 or '3.5' for PY3.
  """
  return {2: '2.7', 3: '3.5'}[sys.version_info.major]


def start_aip_training(input_dict: Dict[Text, List[types.Artifact]],
                       output_dict: Dict[Text, List[types.Artifact]],
                       exec_properties: Dict[Text,
                                             Any], executor_class_path: Text,
                       training_inputs: Dict[Text,
                                             Any], job_id: Optional[Text]):
  """Start a trainer job on AI Platform (AIP).

  This is done by forwarding the inputs/outputs/exec_properties to the
  tfx.scripts.run_executor module on a AI Platform training job interpreter.

  Args:
    input_dict: Passthrough input dict for tfx.components.Trainer.executor.
    output_dict: Passthrough input dict for tfx.components.Trainer.executor.
    exec_properties: Passthrough input dict for tfx.components.Trainer.executor.
    executor_class_path: class path for TFX core default trainer.
    training_inputs: Training input argment for AI Platform training job.
      'pythonModule', 'pythonVersion' and 'runtimeVersion' will be inferred. For
      the full set of parameters, refer to
      https://cloud.google.com/ml-engine/reference/rest/v1/projects.jobs#TrainingInput
    job_id: Job ID for AI Platform Training job. If not supplied,
      system-determined unique ID is given. Refer to
    https://cloud.google.com/ml-engine/reference/rest/v1/projects.jobs#resource-job

  Returns:
    None
  Raises:
    RuntimeError: if the Google Cloud AI Platform training job failed.
  """
  training_inputs = training_inputs.copy()

  json_inputs = artifact_utils.jsonify_artifact_dict(input_dict)
  absl.logging.info('json_inputs=\'%s\'.', json_inputs)
  json_outputs = artifact_utils.jsonify_artifact_dict(output_dict)
  absl.logging.info('json_outputs=\'%s\'.', json_outputs)
  json_exec_properties = json.dumps(exec_properties, sort_keys=True)
  absl.logging.info('json_exec_properties=\'%s\'.', json_exec_properties)

  # Configure AI Platform training job
  api_client = discovery.build('ml', 'v1')

  # We use custom containers to launch training on AI Platform, which invokes
  # the specified image using the container's entrypoint. The default
  # entrypoint for TFX containers is to call scripts/run_executor.py. The
  # arguments below are passed to this run_executor entry to run the executor
  # specified in `executor_class_path`.
  job_args = [
      '--executor_class_path', executor_class_path, '--inputs', json_inputs,
      '--outputs', json_outputs, '--exec-properties', json_exec_properties
  ]

  if not training_inputs.get('masterConfig'):
    training_inputs['masterConfig'] = {
        'imageUri': _TFX_IMAGE,
    }

  training_inputs['args'] = job_args
  training_inputs['labels'] = monitoring_utils.get_labels_dict().update(
      training_inputs.get('labels'), {})

  # Pop project_id so AIP doesn't complain about an unexpected parameter.
  # It's been a stowaway in aip_args and has finally reached its destination.
  project = training_inputs.pop('project')
  project_id = 'projects/{}'.format(project)

  # 'tfx_YYYYmmddHHMMSS' is the default job ID if not explicitly specified.
  job_id = job_id or 'tfx_%s' % datetime.datetime.now().strftime('%Y%m%d%H%M%S')
  job_spec = {'jobId': job_id, 'trainingInput': training_inputs}

  # Submit job to AIP Training
  absl.logging.info(
      'Submitting job=\'{}\', project=\'{}\' to AI Platform.'.format(
          job_id, project))
  request = api_client.projects().jobs().create(
      body=job_spec, parent=project_id)
  request.execute()

  # Wait for AIP Training job to finish
  job_name = '{}/jobs/{}'.format(project_id, job_id)
  request = api_client.projects().jobs().get(name=job_name)
  response = request.execute()
  while response['state'] not in ('SUCCEEDED', 'FAILED'):
    time.sleep(_POLLING_INTERVAL_IN_SECONDS)
    response = request.execute()

  if response['state'] == 'FAILED':
    err_msg = 'Job \'{}\' did not succeed.  Detailed response {}.'.format(
        job_name, response)
    absl.logging.error(err_msg)
    raise RuntimeError(err_msg)

  # AIP training complete
  absl.logging.info('Job \'{}\' successful.'.format(job_name))


def deploy_model_for_aip_prediction(serving_path: Text, model_version: Text,
                                    ai_platform_serving_args: Dict[Text, Any]):
  """Deploys a model for serving with AI Platform.

  Args:
    serving_path: The path to the model. Must be a GCS URI.
    model_version: Version of the model being deployed. Must be different from
      what is currently being served.
    ai_platform_serving_args: Dictionary containing arguments for pushing to AI
      Platform. For the full set of parameters supported, refer to
      https://cloud.google.com/ml-engine/reference/rest/v1/projects.models.versions#Version

  Raises:
    RuntimeError: if an error is encountered when trying to push.
  """
  absl.logging.info(
      'Deploying to model with version {} to AI Platform for serving: {}'
      .format(model_version, ai_platform_serving_args))

  model_name = ai_platform_serving_args['model_name']
  project_id = ai_platform_serving_args['project_id']
  regions = ai_platform_serving_args.get('regions', [])
  runtime_version = _get_tf_runtime_version()
  python_version = _get_caip_python_version()

  api = discovery.build('ml', 'v1')
  body = {'name': model_name, 'regions': regions}
  parent = 'projects/{}'.format(project_id)
  try:
    api.projects().models().create(body=body, parent=parent).execute()
  except errors.HttpError as e:
    # If the error is to create an already existing model, it's ok to ignore.
    # TODO(b/135211463): Remove the disable once the pytype bug is fixed.
    if e.resp.status == 409:  # pytype: disable=attribute-error
      absl.logging.warn('Model {} already exists'.format(model_name))
    else:
      raise RuntimeError('AI Platform Push failed: {}'.format(e))

  body = {
      'name': 'v{}'.format(model_version),
      'regions': regions,
      'deployment_uri': serving_path,
      'runtime_version': runtime_version,
      'python_version': python_version,
  }

  # Push to AIP, and record the operation name so we can poll for its state.
  model_name = 'projects/{}/models/{}'.format(project_id, model_name)
  response = api.projects().models().versions().create(
      body=body, parent=model_name).execute()
  op_name = response['name']

  while True:
    deploy_status = api.projects().operations().get(name=op_name).execute()
    if deploy_status.get('done'):
      # Set the new version as default.
      api.projects().models().versions().setDefault(
          name='{}/versions/{}'.format(model_name, deploy_status['response']
                                       ['name'])).execute()
      break
    if 'error' in deploy_status:
      # The operation completed with an error.
      absl.logging.error(deploy_status['error'])
      raise RuntimeError(
          'Failed to deploy model to AI Platform for serving: {}'.format(
              deploy_status['error']))

    time.sleep(_POLLING_INTERVAL_IN_SECONDS)
    absl.logging.info('Model still being deployed...')

  absl.logging.info(
      'Successfully deployed model {} with version {}, serving from {}'.format(
          model_name, model_version, serving_path))
