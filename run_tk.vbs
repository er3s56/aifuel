' aifuel - launch the tkinter widget without a console window.
'
' NOTE: keep this file pure ASCII. WScript reads .vbs using the system ANSI
' code page, not UTF-8, so non-ASCII bytes get mis-decoded and can swallow a
' closing quote, producing "unterminated string constant".
Launch "widget_tk.py"

Sub Launch(script)
    Dim sh, fso, dir, exe
    Set sh = CreateObject("WScript.Shell")
    Set fso = CreateObject("Scripting.FileSystemObject")
    dir = fso.GetParentFolderName(WScript.ScriptFullName)
    sh.CurrentDirectory = dir

    ' Prefer a real interpreter on PATH. Zero-byte entries are Windows
    ' "app execution aliases" (reparse points) -- invoking those by full path
    ' fails, they only work when the shell resolves them by name.
    exe = FindRealOnPath(sh, fso, "pythonw.exe")
    If exe = "" Then exe = FindRealOnPath(sh, fso, "pyw.exe")

    On Error Resume Next
    If exe <> "" Then
        sh.Run """" & exe & """ """ & dir & "\" & script & """", 0, False
    Else
        sh.Run "pyw """ & dir & "\" & script & """", 0, False
        If Err.Number <> 0 Then
            Err.Clear
            sh.Run "pythonw """ & dir & "\" & script & """", 0, False
        End If
    End If

    If Err.Number <> 0 Then
        MsgBox "aifuel could not start: no usable Python found." & vbCrLf & _
               "Install Python, or run the bundled dist\aifuel\aifuel.exe instead.", _
               48, "aifuel"
    End If
End Sub

Function FindRealOnPath(sh, fso, name)
    Dim parts, i, candidate
    FindRealOnPath = ""
    parts = Split(sh.ExpandEnvironmentStrings("%PATH%"), ";")
    For i = 0 To UBound(parts)
        If Len(Trim(parts(i))) > 0 Then
            candidate = fso.BuildPath(Trim(parts(i)), name)
            If fso.FileExists(candidate) Then
                If fso.GetFile(candidate).Size > 0 Then
                    FindRealOnPath = candidate
                    Exit Function
                End If
            End If
        End If
    Next
End Function
