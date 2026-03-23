@echo off
:: ============================================================
:: Quant Trading Bot — Windows 11 Live Trading Launcher
:: Double-click this file to start the live trading bot.
:: ============================================================

title Quant Trading Bot - LIVE

:: Change to the project root directory (parent of this script)
cd /d "%~dp0.."

echo ============================================================
echo  WARNING: LIVE TRADING MODE - REAL MONEY
echo ============================================================
echo.
set /p CONFIRM="Are you ABSOLUTELY SURE you want to trade live? (type YES): "

if /i not "%CONFIRM%"=="YES" (
    echo Live trading cancelled.
    pause
    exit /b 0
)

echo.
echo ============================================================
echo  SELECT ACCOUNT SIZE CONFIG
echo ============================================================
echo  1) $100
echo  2) $1,000
echo  3) $5,000
echo  4) $10,000
echo  5) $25,000
echo ============================================================
set /p CHOICE="Enter choice (1-5) [Default: 3]: "

set CONFIG_FILE=config\config_live_5000.yaml
if "%CHOICE%"=="1" set CONFIG_FILE=config\config_live_100.yaml
if "%CHOICE%"=="2" set CONFIG_FILE=config\config_live_1000.yaml
if "%CHOICE%"=="3" set CONFIG_FILE=config\config_live_5000.yaml
if "%CHOICE%"=="4" set CONFIG_FILE=config\config_live_10000.yaml
if "%CHOICE%"=="5" set CONFIG_FILE=config\config_live_25000.yaml

echo.
echo Starting trading bot...
echo Config: %CONFIG_FILE%
echo.

:: Use 'python' (Windows standard) not 'python3'
python src\main.py --config %CONFIG_FILE% --env live

:: If the bot exits, pause so you can read any error messages
echo.
echo Bot stopped. Press any key to close...
pause > nul
