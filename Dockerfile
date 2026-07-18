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
COPY us_cities.csv .

# Expose Streamlit port
EXPOSE 8501

# Health check for web server
HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1

# Default: run web server
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_SERVER_ENABLE_CORS=false

CMD ["streamlit", "run", "web.py", "--server.port=8501", "--server.address=0.0.0.0"]
