@echo off
title SWGTCG Standalone
rem Directory-independent: %~dp0 is this file's own folder (with trailing backslash).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1"
