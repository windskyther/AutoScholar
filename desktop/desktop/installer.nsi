Unicode true
Name "AutoScholar"
OutFile "${OUTPUT}"
RequestExecutionLevel user
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoScholar"
!ifdef DEFAULT_ROOT
  InstallDir "${DEFAULT_ROOT}\AutoScholar"
!else
  InstallDir "$LOCALAPPDATA\Programs\AutoScholar"
!endif
InstallDirRegKey HKCU "${UNINSTALL_KEY}" "InstallLocation"
SetCompressor /SOLID lzma
!include "LogicLib.nsh"
!include "x64.nsh"
!include "MUI2.nsh"
!include "FileFunc.nsh"
Var DataDir
!insertmacro MUI_PAGE_DIRECTORY
!define MUI_DIRECTORYPAGE_VARIABLE $DataDir
!define MUI_DIRECTORYPAGE_TEXT_TOP "选择存放资料库、密钥、模型缓存和窗口数据的专用文件夹。使用现有资料请选择原数据目录。"
!define MUI_DIRECTORYPAGE_TEXT_DESTINATION "AutoScholar 数据目录"
!insertmacro MUI_PAGE_DIRECTORY
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
  ReadRegStr $DataDir HKCU "${UNINSTALL_KEY}" "DataLocation"
  ${If} $DataDir == ""
    !ifdef DEFAULT_ROOT
      StrCpy $DataDir "${DEFAULT_ROOT}\AutoScholarData"
    !else
      StrCpy $DataDir "$LOCALAPPDATA\local.autoscholar.workbench"
    !endif
  ${EndIf}
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/DATADIR=" $1
  IfErrors +2
  StrCpy $DataDir $1
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
  System::Call 'kernel32::GetFullPathNameW(w "$INSTDIR", i ${NSIS_MAX_STRLEN}, w .r0, p 0) i.r2'
  IntCmp $2 0 invalidPaths
  System::Call 'kernel32::GetFullPathNameW(w "$DataDir", i ${NSIS_MAX_STRLEN}, w .r1, p 0) i.r2'
  IntCmp $2 0 invalidPaths
  StrCmp $0 "" invalidPaths
  StrCmp $1 "" invalidPaths
  StrCmp $0 "$INSTDIR" 0 invalidPaths
  StrCmp $1 "$DataDir" 0 invalidPaths
  System::Call 'shlwapi::PathIsRootW(w "$INSTDIR") i.r0'
  IntCmp $0 0 +2
  Goto invalidPaths
  System::Call 'shlwapi::PathIsRootW(w "$DataDir") i.r0'
  IntCmp $0 0 +2
  Goto invalidPaths
  System::Call 'shlwapi::PathIsPrefixW(w "$INSTDIR", w "$DataDir") i.r0'
  IntCmp $0 0 +2
  Goto invalidPaths
  System::Call 'shlwapi::PathIsPrefixW(w "$DataDir", w "$INSTDIR") i.r0'
  IntCmp $0 0 +2
  Goto invalidPaths
  IfFileExists "$INSTDIR\incoming" invalidPaths
  IfFileExists "$INSTDIR\previous" invalidPaths
  IfFileExists "$INSTDIR\app\AutoScholar.exe" existing
  IfFileExists "$INSTDIR\*.*" 0 checkData
  System::Call 'shlwapi::PathIsDirectoryEmptyW(w "$INSTDIR") i.r0'
  IntCmp $0 0 invalidPaths
  Goto checkData
existing:
  IfFileExists "$INSTDIR\Uninstall.exe" checkData invalidPaths
checkData:
  IfFileExists "$DataDir\key.dpapi" prepare
  IfFileExists "$DataDir\*.*" 0 prepare
  System::Call 'shlwapi::PathIsDirectoryEmptyW(w "$DataDir") i.r0'
  IntCmp $0 0 invalidPaths
prepare:
  ClearErrors
  CreateDirectory "$DataDir"
  GetTempFileName $0 "$DataDir"
  IfErrors invalidPaths
  Delete "$0"
  SetOutPath "$INSTDIR\incoming"
  ClearErrors
  File /r "${STAGE}\*"
  IfErrors failed
  FileOpen $0 "$INSTDIR\incoming\profile-path.txt" w
  IfErrors failed
  FileWriteUTF16LE $0 "$DataDir"
  FileClose $0
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
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DataLocation" "$DataDir"
  Goto done
invalidPaths:
  MessageBox MB_ICONSTOP "请选择两个独立的专用文件夹。使用空目录，或选择现有的 AutoScholar 安装目录和数据目录。" /SD IDOK
  Abort
done:
SectionEnd

Section "Uninstall"
  ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "InstallLocation"
  StrCmp $0 "$INSTDIR" 0 removeProgram
  Delete "$SMPROGRAMS\AutoScholar.lnk"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoScholar"
removeProgram:
  RMDir /r "$INSTDIR\app"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
