# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Duke is a new AI chatbot for Ekylibre (per `README.md`).

## Current state

The repository is a fresh scaffold — only `README.md`, `LICENSE`, and a Python-oriented `.gitignore` exist. There is no source code, dependency manifest, build config, or test suite yet. The `.gitignore` patterns (uv, poetry, pdm, pipenv, Django, Flask, Celery, Streamlit, Marimo, Jupyter) suggest Python is the intended stack but no choice has been committed.

When the user asks for the first implementation work, confirm the stack (package manager, framework) before generating files — do not infer it from the `.gitignore` alone, which is a generic Python template.

## Conventions to follow once code lands

- Update this file as soon as build/test/lint commands and the high-level architecture are established. Future Claude instances rely on it for orientation.
- Keep additions specific to *this* repo (commands that are non-obvious, architectural decisions that span multiple files). Don't restate generic Python practices.
