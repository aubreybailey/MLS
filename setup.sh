#!/bin/bash
# Setup script for local development
# Creates conda environment from environment.yml

set -e

echo "Creating conda environment 'rental-search'..."
conda env create -f environment.yml || conda env update -f environment.yml

echo ""
echo "Setup complete! To use:"
echo ""
echo "  conda activate rental-search"
echo "  ln -sf ../data data  # if not already linked"
echo ""
echo "  # Web UI"
echo "  streamlit run web.py"
echo ""
echo "  # CLI"
echo "  python search.py \"Providence, RI\""
