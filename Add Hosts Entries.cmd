@echo off
rem One-time setup: point the client's login host at the bundled local server (127.0.0.1).
rem The PowerShell script self-elevates (UAC) and only adds lines that are not already present.
title SWGTCG - Add Hosts Entries
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_ext\add-hosts.ps1"
