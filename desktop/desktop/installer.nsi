Unicode true
Name "AutoScholar"
OutFile "${OUTPUT}"
RequestExecutionLevel user
InstallDir "$LOCALAPPDATA\Programs\AutoScholar"
SetCompressor /SOLID lzma
!include "LogicLib.nsh"
!include "x64.nsh"
!include "MUI2.nsh"
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    Abort "AutoScholar requires 64-bit Windows."
  ${EndIf}
  SetRegView 32
  ReadRegStr $0 HKLM "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${If} $0 == ""
  ${OrIf} $0 == "0.0.0.0"
    ReadRegStr $0 HKCU "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
    ${If} $0 == ""
    ${OrIf} $0 == "0.0.0.0"
      MessageBox MB_OK "Install Microsoft Edge WebView2 Evergreen Runtime, then run AutoScholar Setup again." /SD IDOK
      IfSilent +2
      ExecShell "open" "https://developer.microsoft.com/microsoft-edge/webview2/"
      Abort
    ${EndIf}
  ${EndIf}
FunctionEnd

Section "AutoScholar"
  SetShellVarContext current
  SetOutPath "$INSTDIR\incoming"
  ClearErrors
  File /r "${STAGE}\*"
  IfErrors failed
  SetOutPath "$INSTDIR"
  IfFileExists "$INSTDIR\app\AutoScholar.exe" 0 activate
  ClearErrors
  Rename "$INSTDIR\app" "$INSTDIR\previous"
  IfErrors failed
activate:
  ClearErrors
  Rename "$INSTDIR\incoming" "$INSTDIR\app"
  IfErrors rollback
  RMDir /r "$INSTDIR\previous"
  Goto registered
rollback:
  Rename "$INSTDIR\previous" "$INSTDIR\app"
failed:
  RMDir /r "$INSTDIR\incoming"
  Abort "Close AutoScholar and retry setup."
registered:
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortcut "$SMPROGRAMS\AutoScholar.lnk" "$INSTDIR\app\AutoScholar.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoScholar" "DisplayName" "AutoScholar"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoScholar" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoScholar" "DisplayVersion" "0.1.0"
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\AutoScholar.lnk"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoScholar"
  RMDir /r "$INSTDIR\app"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
