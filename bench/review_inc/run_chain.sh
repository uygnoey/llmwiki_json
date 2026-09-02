#!/bin/sh
cd "$(dirname "$0")/../.."
until [ -f bench/results_review_inc/identity.json ] || ! pgrep -f "bench/review_inc/identity.py" >/dev/null; do sleep 5; done
echo "identity finished $(date)"
python3 bench/review_inc/cost.py > bench/results_review_inc/cost.log 2>&1; echo "cost exit=$? $(date)"
python3 bench/review_inc/mdcost.py > bench/results_review_inc/mdcost.log 2>&1; echo "mdcost exit=$? $(date)"
echo CHAIN_DONE
