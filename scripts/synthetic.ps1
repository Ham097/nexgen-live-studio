param([string]$Output = "$PSScriptRoot\..\private\bench")
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
New-Item -ItemType Directory -Path $Output -Force | Out-Null
$voice = [System.Speech.Synthesis.SpeechSynthesizer]::new()
$voice.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::NotSet,
    [System.Speech.Synthesis.VoiceAge]::NotSet, 0, [System.Globalization.CultureInfo]::new('en-US'))
$format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)
$cases = [ordered]@{
    answer = 'I would choose the train because I can work during the journey. On the other hand, the plane is faster. If we had more time, we could visit another city.'
    beginner = 'Yes, I can.'
    hesitation = 'I think we should take the train.'
}
try {
    foreach ($item in $cases.GetEnumerator()) {
        $path = Join-Path $Output "$($item.Key).wav"
        $voice.SetOutputToWaveFile([IO.Path]::GetFullPath($path), $format)
        if ($item.Key -eq 'hesitation') {
            $voice.SpeakSsml('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">I think <break time="1700ms" /> we should take the train.</speak>')
        } else {
            $voice.Speak($item.Value)
        }
        $voice.SetOutputToNull()
        Set-Content -LiteralPath (Join-Path $Output "$($item.Key).txt") -Value $item.Value -Encoding UTF8
    }
} finally {
    $voice.Dispose()
}
