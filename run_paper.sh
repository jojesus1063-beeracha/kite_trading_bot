#!/bin/bash
cd /home/ubuntu/fno_trading_bot
source venv/bin/activate
export FNO_ACCESS_TOKEN_FILE=/home/ubuntu/kite_trading_bot/access_token.txt
export FNO_TRADING_CAPITAL=20000
export FNO_MODE=PAPER
python3 -m fno_bot.launcher
