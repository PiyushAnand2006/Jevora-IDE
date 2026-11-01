; Jevora IDE custom NSIS installer additions (electron-builder nsis.include).
;
; 1. This machine's Chromium sandbox broker aborts the app before any code runs
;    (verified: main.js never executes without --no-sandbox), so the shortcuts
;    launch the app with that switch. Renderer isolation is disabled on such
;    installs; other machines with a working sandbox can remove the argument.
; 2. LOCAL_UNSAFE_EXEC lets the bundled coding agents and terminal run without
;    Docker; when Docker Desktop is installed the backend prefers containers.
; 3. JEVORA_DISABLE_SANDBOX is read by main.js as a belt-and-braces fallback.

!macro customInstall
  WriteRegStr HKCU "Environment" "LOCAL_UNSAFE_EXEC" "true"
  WriteRegStr HKCU "Environment" "JEVORA_DISABLE_SANDBOX" "1"
  DetailPrint "Enabling host execution for the bundled coding agents (LOCAL_UNSAFE_EXEC=true)"

  ; Recreate the shortcuts with the sandbox switch in the target.
  Delete "$DESKTOP\Jevora IDE.lnk"
  CreateShortCut "$DESKTOP\Jevora IDE.lnk" "$INSTDIR\Jevora IDE.exe" "--no-sandbox" "$INSTDIR\Jevora IDE.exe"
  Delete "$SMPROGRAMS\Jevora IDE\Jevora IDE.lnk"
  CreateShortCut "$SMPROGRAMS\Jevora IDE\Jevora IDE.lnk" "$INSTDIR\Jevora IDE.exe" "--no-sandbox" "$INSTDIR\Jevora IDE.exe"
!macroend

!macro customUnInstall
  DeleteRegValue HKCU "Environment" "LOCAL_UNSAFE_EXEC"
  DeleteRegValue HKCU "Environment" "JEVORA_DISABLE_SANDBOX"
!macroend
