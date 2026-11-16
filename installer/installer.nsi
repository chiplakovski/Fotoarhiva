; Фотоархива — Windows инсталер
Unicode true
SetCompressor /SOLID lzma
SetCompressorDictSize 64
!include "MUI2.nsh"
!include "FileFunc.nsh"

!define APPNAME "Фотоархива"
!define APPKEY "Fotoarhiva"
!define VERSION "2.0.0"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPKEY}"

Name "${APPNAME}"
OutFile "Fotoarhiva-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\Fotoarhiva"
InstallDirRegKey HKCU "Software\${APPKEY}" "InstallDir"
RequestExecutionLevel user
BrandingText "${APPNAME} ${VERSION}"
VIProductVersion "2.0.0.0"
VIAddVersionKey /LANG=0 "ProductName" "Fotoarhiva"
VIAddVersionKey /LANG=0 "FileDescription" "Fotoarhiva Setup"
VIAddVersionKey /LANG=0 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=0 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=0 "LegalCopyright" "Fotoarhiva"

!define MUI_ICON "stage\fotoarhiva.ico"
!define MUI_UNICON "stage\fotoarhiva.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Добредојде во инсталацијата на Фотоархива"
!define MUI_WELCOMEPAGE_TEXT "Фотоархива ги собира сите слики од компјутерот на едно место, ги средува по година и месец и препознава што има на нив.$\r$\n$\r$\nСè што е потребно е вклучено во инсталацијата. Не е потребна администраторска дозвола.$\r$\n$\r$\nСликите никогаш не се праќаат на интернет.$\r$\n$\r$\nКликни Напред за да продолжиш."
!define MUI_FINISHPAGE_TITLE "Фотоархива е инсталирана"
!define MUI_FINISHPAGE_TEXT "Иконата Фотоархива е додадена на Desktop и во Start менито."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Отвори ја Фотоархива сега"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Macedonian"

!macro StopApp
  DetailPrint "Ја исклучувам Фотоархива ако работи..."
  nsExec::Exec 'powershell -NoProfile -ExecutionPolicy Bypass -Command "try{Invoke-WebRequest -UseBasicParsing -Method Post -Body ''{}'' -ContentType ''application/json'' http://127.0.0.1:8765/api/quit -TimeoutSec 2 | Out-Null}catch{}; Start-Sleep -Milliseconds 800; Get-CimInstance Win32_Process | Where-Object { $$_.Name -like ''python*'' -and $$_.CommandLine -like ''*fotoarhiva.py*'' } | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }"'
  Pop $0
  Sleep 800
!macroend

Function LaunchApp
  SetOutPath "$INSTDIR\app"
  Exec '"$INSTDIR\runtime\pythonw.exe" "$INSTDIR\app\fotoarhiva.py" run'
FunctionEnd

Section "Фотоархива" SecMain
  SectionIn RO
  !insertmacro StopApp

  ; чиста надградба: стариот програмски дел се заменува, поставките и сликите остануваат
  RMDir /r "$INSTDIR\runtime"
  RMDir /r "$INSTDIR\app"

  ; остатоци од претходните верзии со start.bat
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

  DetailPrint "Ги копирам датотеките..."
  SetOutPath "$INSTDIR\runtime"
  File /r "stage\runtime\*.*"
  SetOutPath "$INSTDIR\app"
  File /r "stage\app\*.*"
  SetOutPath "$INSTDIR"
  File "stage\fotoarhiva.ico"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; кратенки (работната папка е app)
  SetOutPath "$INSTDIR\app"
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\runtime\pythonw.exe" '"$INSTDIR\app\fotoarhiva.py" run' "$INSTDIR\fotoarhiva.ico" 0 SW_SHOWNORMAL "" "${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}.lnk" "$INSTDIR\runtime\pythonw.exe" '"$INSTDIR\app\fotoarhiva.py" run' "$INSTDIR\fotoarhiva.ico" 0 SW_SHOWNORMAL "" "${APPNAME}"
  ; ако беше вклучено „стартувај со Windows“, кратенката се обновува кон новата локација
  IfFileExists "$SMSTARTUP\${APPNAME}.lnk" 0 +2
    CreateShortcut "$SMSTARTUP\${APPNAME}.lnk" "$INSTDIR\runtime\pythonw.exe" '"$INSTDIR\app\fotoarhiva.py" run --no-browser' "$INSTDIR\fotoarhiva.ico"

  ; „Apps & features“
  WriteRegStr HKCU "Software\${APPKEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "Фотоархива"
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

Section "Uninstall"
  !insertmacro StopApp
  Delete "$DESKTOP\${APPNAME}.lnk"
  Delete "$SMPROGRAMS\${APPNAME}.lnk"
  Delete "$SMSTARTUP\${APPNAME}.lnk"
  RMDir /r "$INSTDIR\runtime"
  RMDir /r "$INSTDIR\app"
  Delete "$INSTDIR\fotoarhiva.ico"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTKEY}"
  DeleteRegKey HKCU "Software\${APPKEY}"
  MessageBox MB_YESNO|MB_ICONQUESTION "Да се избришат и поставките и моделот за препознавање?$\r$\n$\r$\nТвоите слики и средената библиотека НЕ се бришат." /SD IDNO IDNO +2
    RMDir /r "$LOCALAPPDATA\Fotoarhiva"
SectionEnd
