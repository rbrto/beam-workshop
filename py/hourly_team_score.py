#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Second in a series of four pipelines that tell the story of a mobile game.

In addition to the concepts introduced in `user_score`, new concepts include:
windowing and element timestamps; use of `Filter`; using standalone DoFns.

This pipeline processes data collected from gaming events in batch, building on
`user_score` but using fixed windows. It calculates the sum of scores per team,
for each window, optionally allowing specification of two timestamps before and
after which data is filtered out. This allows a model where late data collected
after the intended analysis window can be included, and any late-arriving data
prior to the beginning of the analysis window can be removed as well. By using
windowing and adding element timestamps, we can do finer-grained analysis than
with the `user_score` pipeline. However, our batch processing is high-latency,
in that we don't get results from plays at the beginning of the batch's time
period until the batch is processed.

Optionally include the `--input` argument to specify a batch input file. To
indicate a time after which the data should be filtered out, include the
`--stop_min` arg. E.g., `--stop_min=2015-10-18-23-59` indicates that any data
timestamped after 23:59 PST on 2015-10-18 should not be included in the
analysis. To indicate a time before which data should be filtered out, include
the `--start_min` arg. If you're using the default input
"gs://dataflow-samples/game/gaming_data*.csv", then
`--start_min=2015-11-16-16-10 --stop_min=2015-11-17-16-10` are good values.

For a description of the usage and options, use -h or --help.

To specify a different runner:
  --runner YOUR_RUNNER

NOTE: When specifying a different runner, additional runner-specific options
      may have to be passed in as well

EXAMPLES
--------

# DirectRunner
python hourly_team_score.py \
    --project $PROJECT_ID \
    --dataset $BIGQUERY_DATASET

# DataflowRunner
python hourly_team_score.py \
    --project $PROJECT_ID \
    --dataset $BIGQUERY_DATASET \
    --runner DataflowRunner \
    --temp_location gs://$BUCKET/user_score/temp
"""

from __future__ import absolute_import
from __future__ import print_function

import argparse
import csv
import logging
import sys
import time
from datetime import datetime

import apache_beam as beam
from apache_beam.metrics.metric import Metrics
from apache_beam.options.pipeline_options import GoogleCloudOptions
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions


def str2timestamp(s, fmt='%Y-%m-%d-%H-%M'):
  """Converts a string into a unix timestamp."""
  dt = datetime.strptime(s, fmt)
  epoch = datetime.utcfromtimestamp(0)
  return (dt - epoch).total_seconds()


def timestamp2str(t, fmt='%Y-%m-%d %H:%M:%S.000'):
  """Converts a unix timestamp into a formatted string."""
  return datetime.fromtimestamp(t).strftime(fmt)


class ParseGameEventFn(beam.DoFn):
  """Parses the raw game event info into a Python dictionary.

  Each event line has the following format:
    username,teamname,score,timestamp_in_ms,readable_time

  e.g.:
    user2_AsparagusPig,AsparagusPig,10,1445230923951,2015-11-02 09:09:28.224

  The human-readable time string is not used here.
  """
  def __init__(self):
    super(ParseGameEventFn, self).__init__()
    self.num_parse_errors = Metrics.counter(self.__class__, 'num_parse_errors')

  def process(self, elem):
    try:
      row = list(csv.reader([elem]))[0]
      yield {
          'user': row[0],
          'team': row[1],
          'score': int(row[2]),
          'timestamp': int(row[3]) / 1000.0,
      }
    except:
      # Log and count parse errors
      self.num_parse_errors.inc()
      logging.error('Parse error on "%s"', elem)


class ExtractAndSumScore(beam.PTransform):
  """A transform to extract key/score information and sum the scores.
  The constructor argument `field` determines whether 'team' or 'user' info is
  extracted.
  """
  def __init__(self, field):
    super(ExtractAndSumScore, self).__init__()
    self.field = field

  def expand(self, pcoll):
    return (pcoll
            | beam.Map(lambda elem: (elem[self.field], elem['score']))
            | beam.CombinePerKey(sum))


class TeamScoresDict(beam.DoFn):
  """Formats the data into a dictionary of BigQuery columns with their values

  Receives a (team, score) pair, extracts the window start timestamp, and
  formats everything together into a dictionary. The dictionary is in the format
  {'bigquery_column': value}
  """
  def process(self, team_score, window=beam.DoFn.WindowParam):
    team, score = team_score
    start = timestamp2str(int(window.start))
    yield {
        'team': team,
        'total_score': score,
        'window_start': start,
        'processing_time': timestamp2str(int(time.time()))
    }


class IntoWindows(beam.PTransform):

  def __init__(self, duration):
    super(IntoHourlyWindows, self).__init__()
    self.window_duration_in_seconds = duration

  def expand(self, pcoll):
    return (
        pcoll
        | 'ParseGameEventFn' >> beam.ParDo(ParseGameEventFn())

        # Add an element timestamp based on the event log, and apply fixed
        # windowing.
        | 'AddEventTimestamps' >> beam.Map(
            lambda elem: beam.window.TimestampedValue(elem, elem['timestamp']))
        | 'FixedWindowsTeam' >> beam.WindowInto(
            beam.window.FixedWindows(self.window_duration_in_seconds)))


def run(argv=None):
  """Main entry point; defines and runs the hourly_team_score pipeline."""
  parser = argparse.ArgumentParser()

  # The default maps to two large Google Cloud Storage files (each ~12GB)
  # holding two subsequent day's worth (roughly) of data.
  parser.add_argument('--input',
                      type=str,
                      default='gs://apache-beam-samples/game/gaming_data*.csv',
                      help='Path to the data file(s) containing game data.')
  parser.add_argument('--outputPrefix',
                      type=str,
                      required=True,
                      help='File prefix to output results.')
  parser.add_argument('--window_duration',
                      type=int,
                      default=60,
                      help='Numeric value of fixed window duration, in minutes')

  args, pipeline_args = parser.parse_known_args(argv)

  options = PipelineOptions(pipeline_args)

  # We use the save_main_session option because one or more DoFn's in this
  # workflow rely on global context (e.g., a module imported at module level).
  options.view_as(SetupOptions).save_main_session = True

  with beam.Pipeline(options=options) as p:
    (p
     | 'ReadInputText' >> beam.io.ReadFromText(args.input)
     | 'HourlyTeamScore' >> IntoWindows(duration=args.window_duration)
     | 'GetKeyValuePairs' >> beam.Map(lambda score: (score['team'], score['score']))
     | 'SumScoresPerTeam' >> beam.CombinePerKey(sum)
     | 'FormatTeamScores' >> beam.Map(str)
     | 'WriteTeamScoreSums' >> beam.io.WriteToText(args.outputPrefix))


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  run()