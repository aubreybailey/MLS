FROM condaforge/miniforge3:latest

WORKDIR /app

# Copy environment file first for caching
COPY environment.yml .

# Create conda environment
RUN mamba env create -f environment.yml && \
    mamba clean -afy

# Activate environment by default
ENV PATH=/opt/conda/envs/rental-search/bin:$PATH
ENV CONDA_DEFAULT_ENV=rental-search

# Copy application code
COPY scripts/ ./scripts/
COPY search.py .
COPY web.py .
COPY notify.py .
COPY db.py .
COPY api.py .
COPY us_cities.csv .
COPY download_data.sh .

# Expose Streamlit port
EXPOSE 8501

# Health check for web server
# Use python, not curl: curl isn't installed in the conda image, so a curl-based
# healthcheck reported "unhealthy" permanently even while the app served fine.
HEALTHCHECK CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8501/_stcore/health',timeout=5).read()==b'ok' else 1)" || exit 1

# Default: run web server
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_SERVER_ENABLE_CORS=false

CMD ["streamlit", "run", "web.py", "--server.port=8501", "--server.address=0.0.0.0"]
