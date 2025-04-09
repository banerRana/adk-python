# Copyright 2025 Google LLC
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

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import logging
import os
import tempfile
from typing import Optional

import click
from fastapi import FastAPI
import uvicorn

from . import cli_deploy
from .cli import run_cli
from .cli_eval import MISSING_EVAL_DEPENDENCIES_MESSAGE
from .fast_api import get_fast_api_app
from .utils import envs
from .utils import logs

logger = logging.getLogger(__name__)


@click.group(context_settings={"max_content_width": 240})
def main():
  """Agent Development Kit CLI tools."""
  pass


@main.group()
def deploy():
  """Deploy Agent."""
  pass


@main.command("run")
@click.option(
    "--save_session",
    type=bool,
    is_flag=True,
    show_default=True,
    default=False,
    help="Optional. Whether to save the session to a json file on exit.",
)
@click.argument(
    "agent",
    type=click.Path(
        exists=True, dir_okay=True, file_okay=False, resolve_path=True
    ),
)
def cli_run(agent: str, save_session: bool):
  """Run an interactive CLI for a certain agent.

  AGENT: The path to the agent source code folder.

  Example:

    adk run path/to/my_agent
  """
  logs.log_to_tmp_folder()

  agent_parent_folder = os.path.dirname(agent)
  agent_folder_name = os.path.basename(agent)

  asyncio.run(
      run_cli(
          agent_parent_dir=agent_parent_folder,
          agent_folder_name=agent_folder_name,
          save_session=save_session,
      )
  )


@main.command("eval")
@click.argument(
    "agent_module_file_path",
    type=click.Path(
        exists=True, dir_okay=True, file_okay=False, resolve_path=True
    ),
)
@click.argument("eval_set_file_path", nargs=-1)
@click.option("--config_file_path", help="Optional. The path to config file.")
@click.option(
    "--print_detailed_results",
    is_flag=True,
    show_default=True,
    default=False,
    help="Optional. Whether to print detailed results on console or not.",
)
def cli_eval(
    agent_module_file_path: str,
    eval_set_file_path: tuple[str],
    config_file_path: str,
    print_detailed_results: bool,
):
  """Evaluates an agent given the eval sets.

  AGENT_MODULE_FILE_PATH: The path to the __init__.py file that contains a
  module by the name "agent". "agent" module contains a root_agent.

  EVAL_SET_FILE_PATH: You can specify one or more eval set file paths.

  For each file, all evals will be run by default.

  If you want to run only specific evals from a eval set, first create a comma
  separated list of eval names and then add that as a suffix to the eval set
  file name, demarcated by a `:`.

  For example,

  sample_eval_set_file.json:eval_1,eval_2,eval_3

  This will only run eval_1, eval_2 and eval_3 from sample_eval_set_file.json.

  CONFIG_FILE_PATH: The path to config file.

  PRINT_DETAILED_RESULTS: Prints detailed results on the console.
  """
  envs.load_dotenv_for_agent(agent_module_file_path, ".")

  try:
    from .cli_eval import EvalMetric
    from .cli_eval import EvalResult
    from .cli_eval import EvalStatus
    from .cli_eval import get_evaluation_criteria_or_default
    from .cli_eval import get_root_agent
    from .cli_eval import parse_and_get_evals_to_run
    from .cli_eval import run_evals
    from .cli_eval import try_get_reset_func
  except ModuleNotFoundError:
    raise click.ClickException(MISSING_EVAL_DEPENDENCIES_MESSAGE)

  evaluation_criteria = get_evaluation_criteria_or_default(config_file_path)
  eval_metrics = []
  for metric_name, threshold in evaluation_criteria.items():
    eval_metrics.append(
        EvalMetric(metric_name=metric_name, threshold=threshold)
    )

  print(f"Using evaluation creiteria: {evaluation_criteria}")

  root_agent = get_root_agent(agent_module_file_path)
  reset_func = try_get_reset_func(agent_module_file_path)

  eval_set_to_evals = parse_and_get_evals_to_run(eval_set_file_path)

  try:
    eval_results = list(
        run_evals(
            eval_set_to_evals,
            root_agent,
            reset_func,
            eval_metrics,
            print_detailed_results=print_detailed_results,
        )
    )
  except ModuleNotFoundError:
    raise click.ClickException(MISSING_EVAL_DEPENDENCIES_MESSAGE)

  print("*********************************************************************")
  eval_run_summary = {}

  for eval_result in eval_results:
    eval_result: EvalResult

    if eval_result.eval_set_file not in eval_run_summary:
      eval_run_summary[eval_result.eval_set_file] = [0, 0]

    if eval_result.final_eval_status == EvalStatus.PASSED:
      eval_run_summary[eval_result.eval_set_file][0] += 1
    else:
      eval_run_summary[eval_result.eval_set_file][1] += 1
  print("Eval Run Summary")
  for eval_set_file, pass_fail_count in eval_run_summary.items():
    print(
        f"{eval_set_file}:\n  Tests passed: {pass_fail_count[0]}\n  Tests"
        f" failed: {pass_fail_count[1]}"
    )


@main.command("web")
@click.option(
    "--session_db_url",
    help=(
        "Optional. The database URL to store the session.\n\n  - Use"
        " 'agentengine://<agent_engine_resource_id>' to connect to Vertex"
        " managed session service.\n\n  - Use 'sqlite://<path_to_sqlite_file>'"
        " to connect to a SQLite DB.\n\n  - See"
        " https://docs.sqlalchemy.org/en/20/core/engines.html#backend-specific-urls"
        " for more details on supported DB URLs."
    ),
)
@click.option(
    "--port",
    type=int,
    help="Optional. The port of the server",
    default=8000,
)
@click.option(
    "--allow_origins",
    help="Optional. Any additional origins to allow for CORS.",
    multiple=True,
)
@click.option(
    "--log_level",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False
    ),
    default="INFO",
    help="Optional. Set the logging level",
)
@click.option(
    "--log_to_tmp",
    is_flag=True,
    show_default=True,
    default=False,
    help=(
        "Optional. Whether to log to system temp folder instead of console."
        " This is useful for local debugging."
    ),
)
@click.option(
    "--trace_to_cloud",
    is_flag=True,
    show_default=True,
    default=False,
    help="Optional. Whether to enable cloud trace for telemetry.",
)
@click.argument(
    "agents_dir",
    type=click.Path(
        exists=True, dir_okay=True, file_okay=False, resolve_path=True
    ),
    default=os.getcwd(),
)
def cli_web(
    agents_dir: str,
    log_to_tmp: bool,
    session_db_url: str = "",
    log_level: str = "INFO",
    allow_origins: Optional[list[str]] = None,
    port: int = 8000,
    trace_to_cloud: bool = False,
):
  """Start a FastAPI server with Web UI for agents.

  AGENTS_DIR: The directory of agents, where each sub-directory is a single
  agent, containing at least `__init__.py` and `agent.py` files.

  Example:

    adk web --session_db_url=[db_url] --port=[port] path/to/agents_dir
  """
  if log_to_tmp:
    logs.log_to_tmp_folder()
  else:
    logs.log_to_stderr()

  logging.getLogger().setLevel(log_level)

  @asynccontextmanager
  async def _lifespan(app: FastAPI):
    click.secho(
        f"""\
+-----------------------------------------------------------------------------+
| ADK Web Server started                                                      |
|                                                                             |
| For local testing, access at http://localhost:{port}.{" "*(29 - len(str(port)))}|
+-----------------------------------------------------------------------------+
""",
        fg="green",
    )
    yield  # Startup is done, now app is running
    click.secho(
        """\
+-----------------------------------------------------------------------------+
| ADK Web Server shutting down...                                             |
+-----------------------------------------------------------------------------+
""",
        fg="green",
    )

  app = get_fast_api_app(
      agent_dir=agents_dir,
      session_db_url=session_db_url,
      allow_origins=allow_origins,
      web=True,
      trace_to_cloud=trace_to_cloud,
      lifespan=_lifespan,
  )
  config = uvicorn.Config(
      app,
      host="0.0.0.0",
      port=port,
      reload=True,
  )

  server = uvicorn.Server(config)
  server.run()


@main.command("api_server")
@click.option(
    "--session_db_url",
    help=(
        "Optional. The database URL to store the session.\n\n  - Use"
        " 'agentengine://<agent_engine_resource_id>' to connect to Vertex"
        " managed session service.\n\n  - Use 'sqlite://<path_to_sqlite_file>'"
        " to connect to a SQLite DB.\n\n  - See"
        " https://docs.sqlalchemy.org/en/20/core/engines.html#backend-specific-urls"
        " for more details on supported DB URLs."
    ),
)
@click.option(
    "--port",
    type=int,
    help="Optional. The port of the server",
    default=8000,
)
@click.option(
    "--allow_origins",
    help="Optional. Any additional origins to allow for CORS.",
    multiple=True,
)
@click.option(
    "--log_level",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False
    ),
    default="INFO",
    help="Optional. Set the logging level",
)
@click.option(
    "--log_to_tmp",
    is_flag=True,
    show_default=True,
    default=False,
    help=(
        "Optional. Whether to log to system temp folder instead of console."
        " This is useful for local debugging."
    ),
)
@click.option(
    "--trace_to_cloud",
    is_flag=True,
    show_default=True,
    default=False,
    help="Optional. Whether to enable cloud trace for telemetry.",
)
# The directory of agents, where each sub-directory is a single agent.
# By default, it is the current working directory
@click.argument(
    "agents_dir",
    type=click.Path(
        exists=True, dir_okay=True, file_okay=False, resolve_path=True
    ),
    default=os.getcwd(),
)
def cli_api_server(
    agents_dir: str,
    log_to_tmp: bool,
    session_db_url: str = "",
    log_level: str = "INFO",
    allow_origins: Optional[list[str]] = None,
    port: int = 8000,
    trace_to_cloud: bool = False,
):
  """Start a FastAPI server for agents.

  AGENTS_DIR: The directory of agents, where each sub-directory is a single
  agent, containing at least `__init__.py` and `agent.py` files.

  Example:

    adk api_server --session_db_url=[db_url] --port=[port] path/to/agents_dir
  """
  if log_to_tmp:
    logs.log_to_tmp_folder()
  else:
    logs.log_to_stderr()

  logging.getLogger().setLevel(log_level)

  config = uvicorn.Config(
      get_fast_api_app(
          agent_dir=agents_dir,
          session_db_url=session_db_url,
          allow_origins=allow_origins,
          web=False,
          trace_to_cloud=trace_to_cloud,
      ),
      host="0.0.0.0",
      port=port,
      reload=True,
  )
  server = uvicorn.Server(config)
  server.run()


@deploy.command("cloud_run")
@click.option(
    "--project",
    type=str,
    help=(
        "Required. Google Cloud project to deploy the agent. When absent,"
        " default project from gcloud config is used."
    ),
)
@click.option(
    "--region",
    type=str,
    help=(
        "Required. Google Cloud region to deploy the agent. When absent,"
        " gcloud run deploy will prompt later."
    ),
)
@click.option(
    "--service_name",
    type=str,
    default="adk-default-service-name",
    help=(
        "Optional. The service name to use in Cloud Run (default:"
        " 'adk-default-service-name')."
    ),
)
@click.option(
    "--app_name",
    type=str,
    default="",
    help=(
        "Optional. App name of the ADK API server (default: the folder name"
        " of the AGENT source code)."
    ),
)
@click.option(
    "--port",
    type=int,
    default=8000,
    help="Optional. The port of the ADK API server (default: 8000).",
)
@click.option(
    "--with_cloud_trace",
    type=bool,
    is_flag=True,
    show_default=True,
    default=False,
    help="Optional. Whether to enable Cloud Trace for cloud run.",
)
@click.option(
    "--with_ui",
    type=bool,
    is_flag=True,
    show_default=True,
    default=False,
    help=(
        "Optional. Deploy ADK Web UI if set. (default: deploy ADK API server"
        " only)"
    ),
)
@click.option(
    "--temp_folder",
    type=str,
    default=os.path.join(
        tempfile.gettempdir(),
        "cloud_run_deploy_src",
        datetime.now().strftime("%Y%m%d_%H%M%S"),
    ),
    help=(
        "Optional. Temp folder for the generated Cloud Run source files"
        " (default: a timestamped folder in the system temp directory)."
    ),
)
@click.argument(
    "agent",
    type=click.Path(
        exists=True, dir_okay=True, file_okay=False, resolve_path=True
    ),
)
def cli_deploy_cloud_run(
    agent: str,
    project: Optional[str],
    region: Optional[str],
    service_name: str,
    app_name: str,
    temp_folder: str,
    port: int,
    with_cloud_trace: bool,
    with_ui: bool,
):
  """Deploys an agent to Cloud Run.

  AGENT: The path to the agent source code folder.

  Example:

    adk deploy cloud_run --project=[project] --region=[region] path/to/my_agent
  """
  try:
    cli_deploy.to_cloud_run(
        agent_folder=agent,
        project=project,
        region=region,
        service_name=service_name,
        app_name=app_name,
        temp_folder=temp_folder,
        port=port,
        with_cloud_trace=with_cloud_trace,
        with_ui=with_ui,
    )
  except Exception as e:
    click.secho(f"Deploy failed: {e}", fg="red", err=True)
