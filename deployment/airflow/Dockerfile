FROM apache/airflow:3.2.2-python3.12

# Keep geospatial dependencies separate from the Airflow runtime and constraints.
USER root
RUN mkdir -p /opt/project /opt/pipeline-venv && chown -R airflow:0 /opt/project /opt/pipeline-venv
USER airflow
RUN pip install --no-cache-dir uv==0.12.2
WORKDIR /opt/project
COPY --chown=airflow:0 pyproject.toml uv.lock README.md ./
COPY --chown=airflow:0 src ./src
ENV UV_PROJECT_ENVIRONMENT=/opt/pipeline-venv
RUN uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python
COPY --chown=airflow:0 configs ./configs
COPY --chown=airflow:0 migrations ./migrations
