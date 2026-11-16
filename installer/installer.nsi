; Фотоархива — Windows инсталер
Unicode true
SetCompressor /SOLID lzma
SetCompressorDictSize 64
!include "MUI2.nsh"
!include "FileFunc.nsh"

!define APPNAME "Фотоархива"
!define APPKEY "Fotoarhiva"
!define VERSION "2.1.1"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPKEY}"

Name "${APPNAME}"
OutFile "Fotoarhiva-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\Fotoarhiva"
InstallDirRegKey HKCU "Software\${APPKEY}" "InstallDir"
RequestExecutionLevel user
BrandingText "${APPNAME} ${VERSION}"
VIProductVersion "2.1.1.0"
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
!define MUI_FINISHPAGE_TEXT "Иконата Фотоархива е додадена на Desktop и во Start менито.$\r$\n$\r$\nПри првото отворање се симнува моделот за паметно препознавање (околу 400 MB)."
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
  ; Ја исклучува Фотоархива (сите верзии) пред да се менуваат датотеките.
  ; Скриптата се пишува во датотека за да нема проблеми со наводници.
  DetailPrint "Ја исклучувам Фотоархива ако работи..."
  FileOpen $9 "$TEMP\fotoarhiva-stop.ps1" w
  FileWriteWord $9 0xFEFF
  FileWriteUTF16LE $9 "$$d = '$INSTDIR'$\r$\n"
  FileWriteUTF16LE $9 "try { Invoke-WebRequest -UseBasicParsing -Method Post -Body '{}' -ContentType 'application/json' -Uri 'http://127.0.0.1:8765/api/quit' -TimeoutSec 2 | Out-Null } catch {}$\r$\n"
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
  ; Ја брише папката. Ако нешто сè уште е заклучено, ја преместува настрана (се брише следниот пат).
  RMDir /r "${DIR}"
  IfFileExists "${DIR}\*.*" 0 ${LBL}
    System::Call 'kernel32::GetTickCount()i.r8'
    Rename "${DIR}" "${DIR}.old$8"
  ${LBL}:
!macroend

Function LaunchApp
  SetOutPath "$INSTDIR\app"
  Exec '"$INSTDIR\runtime\pythonw.exe" "$INSTDIR\app\fotoarhiva.py" run'
FunctionEnd

Section "Фотоархива" SecMain
  SectionIn RO
  !insertmacro StopApp

  ; чиста надградба: стариот програмски дел се заменува, поставките и сликите остануваат
  ; избриши останати стари папки од претходни надградби
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
    MessageBox MB_OK|MB_ICONEXCLAMATION "Фотоархива сè уште работи и не може да се замени.$\r$\nРестартирај го компјутерот и стартувај ја инсталацијата повторно."
    Abort

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
  Delete "$SMPROGRAMS\${APPNAME}.lnk"
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\runtime\pythonw.exe" '"$INSTDIR\app\fotoarhiva.py" run' "$INSTDIR\fotoarhiva.ico" 0 SW_SHOWNORMAL "" "${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Деинсталирај ${APPNAME}.lnk" "$INSTDIR\Uninstall.exe" "" "$INSTDIR\Uninstall.exe" 0
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
  Delete "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk"
  Delete "$SMPROGRAMS\${APPNAME}\Деинсталирај ${APPNAME}.lnk"
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
  MessageBox MB_YESNO|MB_ICONQUESTION "Да се избришат и поставките и моделот за препознавање?$\r$\n$\r$\nТвоите слики и средената библиотека НЕ се бришат." /SD IDNO IDNO +2
    RMDir /r "$LOCALAPPDATA\Fotoarhiva"
SectionEnd
