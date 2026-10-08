@echo off
title HomeServe Ollama Cloudflare Tunnel
echo ========================================================
echo   HomeServe Ollama Cloudflare Tunnel
echo   Exposing local Ollama (http://localhost:11434) to Cloud
echo ========================================================
echo.
echo Make sure Ollama is running in background (ollama run qwen2.5:14b)
echo.
if exist "%~dp0cloudflared.exe" (
    "%~dp0cloudflared.exe" tunnel --url http://localhost:11434
) else (
    echo [ERROR] cloudflared.exe was not found in this folder.
    pause
)
