; Fotoarhiva — Windows installer
Unicode true
SetCompressor /SOLID lzma
SetCompressorDictSize 64
!include "MUI2.nsh"
!include "FileFunc.nsh"

!define APPNAME "Fotoarhiva"
!define OLDNAME "Фотоархива"  ; shortcut name used by versions up to 2.4
!define APPKEY "Fotoarhiva"
!define VERSION "2.8.0"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPKEY}"

Name "${APPNAME}"
OutFile "Fotoarhiva-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\Fotoarhiva"
InstallDirRegKey HKCU "Software\${APPKEY}" "InstallDir"
RequestExecutionLevel user
BrandingText "${APPNAME} ${VERSION}"
VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=0 "ProductName" "Fotoarhiva"
VIAddVersionKey /LANG=0 "FileDescription" "Fotoarhiva Setup"
VIAddVersionKey /LANG=0 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=0 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=0 "LegalCopyright" "Fotoarhiva"

!define MUI_ICON "stage\fotoarhiva.ico"
!define MUI_UNICON "stage\fotoarhiva.ico"
!define MUI_ABORTWARNING
Var OldVersion   ; the installed version when this is an update, otherwise empty
Var WTitle
Var WText
Var FTitle
Var FText
!define MUI_WELCOMEPAGE_TITLE "$WTitle"
!define MUI_WELCOMEPAGE_TEXT "$WText"
!define MUI_FINISHPAGE_TITLE "$FTitle"
!define MUI_FINISHPAGE_TEXT "$FText"
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Open Fotoarhiva now"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp

!insertmacro MUI_PAGE_WELCOME
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfUpdate
!insertmacro MUI_PAGE_DIRECTORY
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfUpdate
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

!macro StopApp
  ; Stops Fotoarhiva (any version) before the files are replaced.
  ; The script is written to a file to avoid quoting problems.
  DetailPrint "Closing Fotoarhiva if it is running..."
  FileOpen $9 "$TEMP\fotoarhiva-stop.ps1" w
  FileWriteWord $9 0xFEFF
  FileWriteUTF16LE $9 "$$d = '$INSTDIR'$\r$\n"
  FileWriteUTF16LE $9 "$$t = Get-Content -Raw (Join-Path $$env:LOCALAPPDATA 'Fotoarhiva\quit.token') -ErrorAction SilentlyContinue$\r$\n"
  FileWriteUTF16LE $9 "try { Invoke-WebRequest -UseBasicParsing -Method Post -Body '{}' -ContentType 'application/json' -Headers @{'X-Quit-Token'=([string]$$t).Trim()} -Uri 'http://127.0.0.1:8765/api/quit' -TimeoutSec 2 | Out-Null } catch {}$\r$\n"
  FileWriteUTF16LE $9 "Start-Sleep -Milliseconds 1000$\r$\n"
  FileWriteUTF16LE $9 "for ($$i = 0; $$i -lt 3; $$i++) {$\r$\n"
  FileWriteUTF16LE $9 "  $$p = Get-CimInstance Win32_Process | Where-Object { ($$_.ExecutablePath -and $$_.ExecutablePath.StartsWith($$d, [StringComparison]::OrdinalIgnoreCase)) -or ($$_.CommandLine -like '*fotoarhiva.py*') }$\r$\n"
  FileWriteUTF16LE $9 "  if (-not $$p) { break }$\r$\n"
  FileWriteUTF16LE $9 "  $$p | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }$\r$\n"
  FileWriteUTF16LE $9 "  Start-Sleep -Milliseconds 800$\r$\n"
  FileWriteUTF16LE $9 "}$\r$\n"
  FileClose $9
  nsExec::Exec 'powershell -NoProfile -ExecutionPolicy Bypass -File "$TEMP\fotoarhiva-stop.ps1"'
  Pop $0
  Delete "$TEMP\fotoarhiva-stop.ps1"
  Sleep 500
!macroend

!macro ClearDir DIR LBL
  ; Deletes the folder. If something is still locked, moves it aside (deleted next time).
  RMDir /r "${DIR}"
  IfFileExists "${DIR}\*.*" 0 ${LBL}
    System::Call 'kernel32::GetTickCount()i.r8'
    Rename "${DIR}" "${DIR}.old$8"
  ${LBL}:
!macroend

Function .onInit
  ReadRegStr $OldVersion HKCU "${UNINSTKEY}" "DisplayVersion"
  ReadRegStr $0 HKCU "Software\${APPKEY}" "InstallDir"
  StrCmp $OldVersion "" fresh
  StrCmp $0 "" fresh
  IfFileExists "$0\app\fotoarhiva.py" 0 fresh
    StrCpy $INSTDIR $0
    StrCpy $WTitle "Update Fotoarhiva"
    StrCpy $WText "Fotoarhiva $OldVersion is installed on this computer. This updates it to version ${VERSION}.$\r$\n$\r$\nYour photos, albums, tags, settings, login and recognition models all stay. Models that are already installed are not downloaded again.$\r$\n$\r$\nNew: people recognition by face, better videos (all formats play, duration, place, recognition) and finding and deleting duplicates.$\r$\n$\r$\nIf Fotoarhiva is open, it is closed for the update.$\r$\n$\r$\nClick Next to update."
    StrCpy $FTitle "Fotoarhiva is updated"
    StrCpy $FText "Fotoarhiva is updated to version ${VERSION}. Your photos and settings are unchanged."
    Goto done
  fresh:
    StrCpy $OldVersion ""
    StrCpy $WTitle "Welcome to the Fotoarhiva setup"
    StrCpy $WText "Fotoarhiva gathers all the photos on your computer in one place, organizes them by year and month and recognizes what is in them.$\r$\n$\r$\nEverything it needs is included. No administrator rights are needed.$\r$\n$\r$\nYour photos are never sent to the internet.$\r$\n$\r$\nClick Next to continue."
    StrCpy $FTitle "Fotoarhiva is installed"
    StrCpy $FText "Fotoarhiva is installed, together with the recognition models.$\r$\n$\r$\nWhen it opens, sign in with admin / 0409979 and choose which folders to import photos from. Recognition starts only after that."
  done:
FunctionEnd

Function SkipIfUpdate
  StrCmp $OldVersion "" +2
    Abort  ; an update keeps the folder and the models: nothing to choose
FunctionEnd

Function LaunchApp
  SetOutPath "$INSTDIR\app"
  Exec '"$INSTDIR\runtime\pythonw.exe" "$INSTDIR\app\fotoarhiva.py" run'
FunctionEnd

Section "Fotoarhiva" SecMain
  SectionIn RO
  !insertmacro StopApp

  ; clean upgrade: the program is replaced, settings and photos stay
  ; remove leftover folders from earlier upgrades
  FindFirst $1 $2 "$INSTDIR\*.old*"
  loop_old:
    StrCmp $2 "" done_old
    RMDir /r "$INSTDIR\$2"
    FindNext $1 $2
    Goto loop_old
  done_old:
  FindClose $1
  !insertmacro ClearDir "$INSTDIR\runtime" cleared_runtime
  !insertmacro ClearDir "$INSTDIR\app" cleared_app
  IfFileExists "$INSTDIR\runtime\*.*" 0 +3
    MessageBox MB_OK|MB_ICONEXCLAMATION "Fotoarhiva is still running and cannot be replaced.$\r$\nRestart the computer and run the setup again."
    Abort

  ; leftovers from the old start.bat versions
  RMDir /r "$LOCALAPPDATA\Fotoarhiva\.venv"
  Delete "$LOCALAPPDATA\Fotoarhiva\fotoarhiva.py"
  Delete "$LOCALAPPDATA\Fotoarhiva\ai.py"
  Delete "$LOCALAPPDATA\Fotoarhiva\places.py"
  Delete "$LOCALAPPDATA\Fotoarhiva\ui.html"
  Delete "$LOCALAPPDATA\Fotoarhiva\cities.csv.gz"
  Delete "$LOCALAPPDATA\Fotoarhiva\README.md"
  Delete "$LOCALAPPDATA\Fotoarhiva\requirements.txt"
  Delete "$LOCALAPPDATA\Fotoarhiva\Fotoarhiva.vbs"
  Delete "$LOCALAPPDATA\Fotoarhiva\.shortcut-done"

  DetailPrint "Copying files..."
  SetOutPath "$INSTDIR\runtime"
  File /r "stage\runtime\*.*"
  SetOutPath "$INSTDIR\app"
  File /r "stage\app\*.*"
  SetOutPath "$INSTDIR"
  File "stage\fotoarhiva.ico"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; old Macedonian-named shortcuts from versions up to 2.4
  Delete "$DESKTOP\${OLDNAME}.lnk"
  Delete "$SMPROGRAMS\${OLDNAME}.lnk"
  Delete "$SMPROGRAMS\${OLDNAME}\${OLDNAME}.lnk"
  Delete "$SMPROGRAMS\${OLDNAME}\${OLDNAME} (безбеден режим).lnk"
  Delete "$SMPROGRAMS\${OLDNAME}\Деинсталирај ${OLDNAME}.lnk"
  RMDir "$SMPROGRAMS\${OLDNAME}"
  IfFileExists "$SMSTARTUP\${OLDNAME}.lnk" 0 +3
    Delete "$SMSTARTUP\${OLDNAME}.lnk"
    CreateShortcut "$SMSTARTUP\${APPNAME}.lnk" "$INSTDIR\runtime\pythonw.exe" '"$INSTDIR\app\fotoarhiva.py" run --no-browser' "$INSTDIR\fotoarhiva.ico"

  ; shortcuts (working folder is app)
  SetOutPath "$INSTDIR\app"
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\runtime\pythonw.exe" '"$INSTDIR\app\fotoarhiva.py" run' "$INSTDIR\fotoarhiva.ico" 0 SW_SHOWNORMAL "" "${APPNAME}"
  Delete "$SMPROGRAMS\${APPNAME}.lnk"
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\runtime\pythonw.exe" '"$INSTDIR\app\fotoarhiva.py" run' "$INSTDIR\fotoarhiva.ico" 0 SW_SHOWNORMAL "" "${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME} (safe mode).lnk" "$INSTDIR\runtime\pythonw.exe" '"$INSTDIR\app\fotoarhiva.py" run --safe' "$INSTDIR\fotoarhiva.ico" 0 SW_SHOWNORMAL "" "${APPNAME} without recognition"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Uninstall ${APPNAME}.lnk" "$INSTDIR\Uninstall.exe" "" "$INSTDIR\Uninstall.exe" 0
  ; if "start with Windows" was on, the shortcut is pointed at the new location
  IfFileExists "$SMSTARTUP\${APPNAME}.lnk" 0 +2
    CreateShortcut "$SMSTARTUP\${APPNAME}.lnk" "$INSTDIR\runtime\pythonw.exe" '"$INSTDIR\app\fotoarhiva.py" run --no-browser' "$INSTDIR\fotoarhiva.ico"

  ; "Apps & features"
  WriteRegStr HKCU "Software\${APPKEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "Fotoarhiva"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\fotoarhiva.ico"
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTKEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINSTKEY}" "EstimatedSize" "$0"
SectionEnd

Section "Recognition models (~3.5 GB download)" SecModels
  DetailPrint "Downloading the recognition models. This takes a few minutes, depending on the internet connection..."
  SetDetailsView show
  SetOutPath "$INSTDIR\app"
  nsExec::ExecToLog '"$INSTDIR\runtime\python.exe" "$INSTDIR\app\fotoarhiva.py" install-models'
  Pop $0
  StrCmp $0 "0" models_ok
    MessageBox MB_OK|MB_ICONINFORMATION "The models were not fully downloaded (check the internet connection).$\r$\nFotoarhiva will finish downloading them when you start it."
  models_ok:
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "The Fotoarhiva program."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecModels} "Quick recognition (SigLIP 2), the photo describer (Qwen3-VL 4B) and face recognition (YuNet + SFace). Downloaded now so the app is complete. If unchecked, they are downloaded later."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  !insertmacro StopApp
  Delete "$DESKTOP\${APPNAME}.lnk"
  Delete "$SMPROGRAMS\${APPNAME}.lnk"
  Delete "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk"
  Delete "$SMPROGRAMS\${APPNAME}\${APPNAME} (safe mode).lnk"
  Delete "$SMPROGRAMS\${APPNAME}\Uninstall ${APPNAME}.lnk"
  RMDir "$SMPROGRAMS\${APPNAME}"
  Delete "$SMSTARTUP\${APPNAME}.lnk"
  RMDir /r "$INSTDIR\runtime"
  RMDir /r "$INSTDIR\app"
  FindFirst $1 $2 "$INSTDIR\*.old*"
  uloop:
    StrCmp $2 "" udone
    RMDir /r "$INSTDIR\$2"
    FindNext $1 $2
    Goto uloop
  udone:
  FindClose $1
  Delete "$INSTDIR\fotoarhiva.ico"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTKEY}"
  DeleteRegKey HKCU "Software\${APPKEY}"
  MessageBox MB_YESNO|MB_ICONQUESTION "Also delete the settings and the recognition models?$\r$\n$\r$\nYour photos and the organized library are NOT deleted." /SD IDNO IDNO +2
    RMDir /r "$LOCALAPPDATA\Fotoarhiva"
SectionEnd
