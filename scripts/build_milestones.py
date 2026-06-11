name: Build playoff leaderboards

on:
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch: {}

permissions:
  contents: write

concurrency:
  group: milestones-build
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Build
        run: python scripts/build_milestones.py
      - name: Commit & push if changed
        run: |
          git config user.name "milestones-bot"
          git config user.email "actions@users.noreply.github.com"
          git add data
          if git diff --cached --quiet; then echo "No changes."; else
            git commit -m "Leaderboards update $(date -u +'%F %H:%M')"; git push; fi
