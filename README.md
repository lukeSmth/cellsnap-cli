# `cellsnap-cli`

Publish rich notebook snapshots without adding .ipynb files to working git history. Sister tool to [jupytext](https://github.com/jupytext/jupytext).

![workflow](assets/workflow.png)

## Installation

**With `uv`:**

`uv tool install cellsnap-cli`

**With `pip`:**

`pip install cellsnap-cli`

## Workflow

1. Use jupytext to sync .ipynb notebooks with .py source format.
2. Push notebook source files (.py) to working branches. 
3. Push notebook artifact files (.ipynb) to dedicated artifact branches: run `cellsnap`

Cellsnap artifact branches only store the latest commit, preventing repo bloat common when committing unstripped .ipynb files. Published notebooks are stamped with frontmatter that refers back to their source and a manifest is added to the artifact branch README.md.
