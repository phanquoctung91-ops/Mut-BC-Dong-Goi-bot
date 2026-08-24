@echo off
set TELEGRAM_BOT_TOKEN=8819674685:AAF_6bJGm-eU3rUAWcTQU-PpAl3YbdSKq20
set TELEGRAM_CHAT_ID=-5599201123
set SHEET_ID=1d_AEVTxGPBgWnRJm8AlYctAC3I9iOv3yJ00EIlPKnFY
cd /d "C:\Users\LAPTOP HP\Mut-BC-Dong-Goi-bot"
python report_bot.py >> run_log.txt 2>&1
