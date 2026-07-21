Option Explicit

Dim fso, shell, processEnvironment
Dim launcherDirectory, pythonExecutable, sourceScript, frozenExecutable
Dim codexPath, pathValue, commandLine, launchError, startupError
Dim codexDirectories

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
Set processEnvironment = shell.Environment("Process")

launcherDirectory = fso.GetParentFolderName(WScript.ScriptFullName)

' Keep both the launcher and the child process rooted at the application folder.
On Error Resume Next
Err.Clear
shell.CurrentDirectory = launcherDirectory
startupError = Err.Description
Err.Clear
On Error GoTo 0
If Len(startupError) > 0 Then
    ShowError "Transcript Quiz could not set its working directory." & vbCrLf & vbCrLf _
        & "Folder: " & launcherDirectory & vbCrLf _
        & "Windows reported: " & startupError
    WScript.Quit 1
End If

' These cover npm's Windows shims and the usual per-user/system standalone
' Codex locations. Only existing folders are added to the child PATH.
codexDirectories = Array( _
    EnvironmentValue(processEnvironment, "APPDATA") & "\npm", _
    EnvironmentValue(processEnvironment, "LOCALAPPDATA") & "\npm", _
    EnvironmentValue(processEnvironment, "LOCALAPPDATA") & "\Programs\nodejs", _
    EnvironmentValue(processEnvironment, "ProgramFiles") & "\nodejs", _
    EnvironmentValue(processEnvironment, "ProgramFiles(x86)") & "\nodejs", _
    EnvironmentValue(processEnvironment, "NVM_SYMLINK"), _
    EnvironmentValue(processEnvironment, "USERPROFILE") & "\.npm-global\bin", _
    EnvironmentValue(processEnvironment, "USERPROFILE") & "\.local\bin", _
    EnvironmentValue(processEnvironment, "USERPROFILE") & "\.codex\bin", _
    EnvironmentValue(processEnvironment, "LOCALAPPDATA") & "\Codex", _
    EnvironmentValue(processEnvironment, "LOCALAPPDATA") & "\Programs\Codex", _
    EnvironmentValue(processEnvironment, "LOCALAPPDATA") & "\OpenAI\Codex", _
    EnvironmentValue(processEnvironment, "LOCALAPPDATA") & "\Programs\OpenAI\Codex", _
    EnvironmentValue(processEnvironment, "ProgramFiles") & "\Codex", _
    EnvironmentValue(processEnvironment, "ProgramFiles") & "\OpenAI\Codex", _
    EnvironmentValue(processEnvironment, "ProgramFiles(x86)") & "\Codex", _
    EnvironmentValue(processEnvironment, "ProgramFiles(x86)") & "\OpenAI\Codex" _
)

pathValue = EnvironmentValue(processEnvironment, "PATH")
AddPathEntry pathValue, fso, launcherDirectory
For Each codexPath In codexDirectories
    AddPathEntry pathValue, fso, codexPath
Next
processEnvironment("PATH") = pathValue

' An explicit value wins. Otherwise pass the first executable found in the
' common locations (including an npm .cmd shim) to the Python application.
codexPath = StripOuterQuotes(Trimmed(EnvironmentValue(processEnvironment, "CODEX_CLI_PATH")))
If Len(codexPath) > 0 Then
    codexPath = StripOuterQuotes(shell.ExpandEnvironmentStrings(codexPath))
    If fso.FileExists(codexPath) Then
        codexPath = fso.GetAbsolutePathName(codexPath)
        AddPathEntry pathValue, fso, fso.GetParentFolderName(codexPath)
        processEnvironment("PATH") = pathValue
    End If
    processEnvironment("CODEX_CLI_PATH") = codexPath
Else
    codexPath = FindCodex(fso, processEnvironment, codexDirectories)
    If Len(codexPath) > 0 Then
        processEnvironment("CODEX_CLI_PATH") = codexPath
    End If
End If

frozenExecutable = fso.BuildPath(launcherDirectory, "TranscriptQuiz.exe")
pythonExecutable = fso.BuildPath(launcherDirectory, ".venv\Scripts\pythonw.exe")
sourceScript = fso.BuildPath(launcherDirectory, "main.py")

If fso.FileExists(frozenExecutable) Then
    commandLine = QuoteArgument(frozenExecutable)
ElseIf fso.FileExists(pythonExecutable) And fso.FileExists(sourceScript) Then
    commandLine = QuoteArgument(pythonExecutable) & " " & QuoteArgument(sourceScript)
Else
    ShowError "Transcript Quiz could not be started." & vbCrLf & vbCrLf _
        & "This launcher must be beside either a complete PyInstaller folder containing:" & vbCrLf _
        & "  TranscriptQuiz.exe" & vbCrLf & vbCrLf _
        & "or a source tree containing both:" & vbCrLf _
        & "  .venv\Scripts\pythonw.exe" & vbCrLf _
        & "  main.py" & vbCrLf & vbCrLf _
        & "Checked folder: " & launcherDirectory
    WScript.Quit 1
End If

' Window style 0 keeps this launcher and the source process console-free.
On Error Resume Next
Err.Clear
shell.Run commandLine, 0, False
launchError = Err.Description
If Err.Number <> 0 And Len(launchError) = 0 Then
    launchError = "Windows Script Host error " & CStr(Err.Number)
End If
Err.Clear
On Error GoTo 0

If Len(launchError) > 0 Then
    ShowError "Transcript Quiz could not be launched." & vbCrLf & vbCrLf _
        & "Target: " & commandLine & vbCrLf _
        & "Windows reported: " & launchError
    WScript.Quit 1
End If

Sub ShowError(message)
    MsgBox message, vbCritical + vbOKOnly, "Transcript Quiz"
End Sub

Function EnvironmentValue(environment, name)
    On Error Resume Next
    Err.Clear
    EnvironmentValue = environment(name)
    If Err.Number <> 0 Then EnvironmentValue = ""
    Err.Clear
    On Error GoTo 0
End Function

Function Trimmed(value)
    If IsNull(value) Or IsEmpty(value) Then
        Trimmed = ""
    Else
        Trimmed = Trim(CStr(value))
    End If
End Function

Function StripOuterQuotes(value)
    value = Trimmed(value)
    If Len(value) >= 2 Then
        If Left(value, 1) = """" And Right(value, 1) = """" Then
            value = Mid(value, 2, Len(value) - 2)
        End If
    End If
    StripOuterQuotes = value
End Function

Sub AddPathEntry(ByRef pathValue, fileSystem, ByVal entry)
    entry = StripOuterQuotes(entry)
    If Len(entry) = 0 Then Exit Sub
    If Not fileSystem.FolderExists(entry) Then Exit Sub
    If PathContains(pathValue, entry) Then Exit Sub

    If Len(Trimmed(pathValue)) = 0 Then
        pathValue = entry
    Else
        pathValue = pathValue & ";" & entry
    End If
End Sub

Function PathContains(pathValue, entry)
    Dim pathPart, normalizedEntry
    PathContains = False
    normalizedEntry = NormalizePathEntry(entry)

    If Len(Trimmed(pathValue)) = 0 Then Exit Function
    For Each pathPart In Split(CStr(pathValue), ";")
        If StrComp(NormalizePathEntry(pathPart), normalizedEntry, vbTextCompare) = 0 Then
            PathContains = True
            Exit Function
        End If
    Next
End Function

Function NormalizePathEntry(value)
    value = StripOuterQuotes(value)
    Do While Len(value) > 3 And Right(value, 1) = "\"
        value = Left(value, Len(value) - 1)
    Loop
    NormalizePathEntry = value
End Function

Function FindCodex(fileSystem, environment, directories)
    Dim names, directory, fileName, candidate, pathParts, pathPart
    names = Array("codex.exe", "codex.cmd", "codex.bat", "codex")

    For Each directory In directories
        directory = StripOuterQuotes(directory)
        If Len(directory) > 0 And fileSystem.FolderExists(directory) Then
            For Each fileName In names
                candidate = fileSystem.BuildPath(directory, fileName)
                If fileSystem.FileExists(candidate) Then
                    FindCodex = fileSystem.GetAbsolutePathName(candidate)
                    Exit Function
                End If
            Next
        End If
    Next

    pathParts = Split(EnvironmentValue(environment, "PATH"), ";")
    For Each pathPart In pathParts
        pathPart = StripOuterQuotes(pathPart)
        If Len(pathPart) > 0 And fileSystem.FolderExists(pathPart) Then
            For Each fileName In names
                candidate = fileSystem.BuildPath(pathPart, fileName)
                If fileSystem.FileExists(candidate) Then
                    FindCodex = fileSystem.GetAbsolutePathName(candidate)
                    Exit Function
                End If
            Next
        End If
    Next

    FindCodex = ""
End Function

Function QuoteArgument(value)
    QuoteArgument = """" & CStr(value) & """"
End Function
