#!/usr/bin/env python3
"""
Test runner script for the agent_tools refactor.

This script sets up a virtual environment, installs dependencies, and runs the test suite.
"""

import subprocess
import sys
import os
import shutil

def run_command(cmd, cwd=None):
    """Run a command and return True if successful."""
    try:
        result = subprocess.run(cmd, shell=True, cwd=cwd, check=True, capture_output=True, text=True)
        print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {cmd}")
        print(f"Error: {e.stderr}")
        return False

def main():
    # Check if Python is available
    if not shutil.which("python"):
        print("Python not found. Please install Python 3.8+")
        sys.exit(1)

    # Create virtual environment
    venv_dir = ".venv"
    if os.path.exists(venv_dir):
        shutil.rmtree(venv_dir)
    print("Creating virtual environment...")
    if not run_command(f"python -m venv {venv_dir}"):
        sys.exit(1)

    # Activate venv and install dependencies
    pip_path = os.path.join(venv_dir, "Scripts", "pip") if os.name == "nt" else os.path.join(venv_dir, "bin", "pip")
    python_path = os.path.join(venv_dir, "Scripts", "python") if os.name == "nt" else os.path.join(venv_dir, "bin", "python")

    print("Installing dependencies...")
    if not run_command(f'"{python_path}" -m pip install --upgrade pip'):
        sys.exit(1)
    if not run_command(f'"{pip_path}" install -r requirements.txt'):
        sys.exit(1)

    # Run tests
    print("Running tests...")
    if not run_command(f'"{python_path}" -m pytest -q'):
        sys.exit(1)

    print("All tests passed!")

if __name__ == "__main__":
    main()