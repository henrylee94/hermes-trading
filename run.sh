#!/bin/bash
cd "$(dirname "$0")"
PYTHON=venv_new/bin/python
case "$1" in
  universe) $PYTHON scripts/module0_universe.py "${@:2}" ;;
  value)    $PYTHON scripts/pipeline_a_value.py ;;
  smart)    $PYTHON scripts/pipeline_c_smart.py ;;
  swing)    $PYTHON scripts/pipeline_b_swing.py "${@:2}" ;;
  server)   $PYTHON -m uvicorn scripts.server:app --host 0.0.0.0 --port 8777 ;;
  bot)      $PYTHON scripts/telegram_bot.py ;;
  *)        echo "Usage: ./run.sh {universe|value|smart|swing|server|bot}" ;;
esac
