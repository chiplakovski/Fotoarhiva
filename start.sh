#!/bin/bash
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 не е инсталиран. Се обидувам да го инсталирам..."
  if command -v brew >/dev/null 2>&1; then brew install python
  elif command -v apt-get >/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip python3-tk
  elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y python3 python3-tkinter
  fi
fi
command -v python3 >/dev/null 2>&1 || { echo "Инсталирај Python од https://www.python.org/downloads/"; exit 1; }
[ -x .venv/bin/python ] || python3 -m venv .venv
P=.venv/bin/python
$P -m pip install -q --disable-pip-version-check "Pillow>=10.0" || { echo "Pillow не можеше да се инсталира."; exit 1; }
$P -m pip install -q --disable-pip-version-check "pillow-heif>=0.16" numpy onnxruntime tokenizers imageio-ffmpeg >/dev/null 2>&1
$P fotoarhiva.py run
