Option Explicit
Dim shell, folder, fso, python, marker, installer, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
python = folder & "\.venv\Scripts\pythonw.exe"
marker = folder & "\.venv\installed-v1"
installer = folder & "\Install NexGen Live Studio.cmd"
If Not fso.FileExists(marker) Or Not fso.FileExists(python) Then
    If shell.Run("cmd.exe /d /c " & Chr(34) & Chr(34) & installer & Chr(34) & Chr(34), 1, True) <> 0 Then
        WScript.Quit 1
    End If
End If
command = Chr(34) & python & Chr(34) & " -m studio"
If WScript.Arguments.Count > 0 Then
    If WScript.Arguments(0) = "--demo" Then command = command & " --demo"
End If
shell.Run command, 0, False
