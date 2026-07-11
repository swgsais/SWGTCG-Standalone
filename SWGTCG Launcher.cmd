@echo off
rem SWGTCG-Standalone -- all-in-one themed GUI launcher.
rem Runs launcher-gui.ps1 in an STA apartment (required by WinForms), console hidden.
rem %~dp0 is this file's own folder (trailing backslash) -- directory-independent.
start "" powershell -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File "%~dp0launcher-gui.ps1"
