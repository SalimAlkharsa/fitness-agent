#!/bin/bash
# Staged to packages/fitness/app/build.sh — DO Functions' Python runtime only
# installs dependencies when the function ships this build script (it creates
# the virtualenv the runtime activates).
set -e
virtualenv --without-pip virtualenv
pip install -r requirements.txt --target virtualenv/lib/python3.11/site-packages
