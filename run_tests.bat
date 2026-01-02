@echo off
python -m venv .venv
call .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -q pytest pytest-asyncio
python -m pytest -q
