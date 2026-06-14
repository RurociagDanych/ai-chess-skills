---
name: visualize-chess-analysis
description: Build a self-contained offline HTML report from an existing analysis artifact. Use when the user wants an interactive chess report, optional text insights embedded into it, or a local artifact prepared for viewing without external services.
allowed-tools:
  - Bash(ai-chess report *)
  - Read
---

# Visualize Chess Analysis

Use this skill only after the analysis artifact already exists and is the right one for the request.

## Generate The Report

Create a self-contained offline HTML file from `analysis.v1.json`:

```bash
ai-chess report PATH_TO_ANALYSIS.json --output REPORT.html
```

If the user has extra narrative notes to embed, include `--insights`:

```bash
ai-chess report PATH_TO_ANALYSIS.json --output REPORT.html --insights NOTES.txt
```

## Constraints

- Treat the report as offline and self-contained. Do not add external assets, scripts, stylesheets, or servers.
- Use the generated HTML as the deliverable and report its path.
- Only open or view the file when the host permits GUI actions and the user has asked for that step.
- If opening is not permitted or not requested, stop after generation and describe the output path.
