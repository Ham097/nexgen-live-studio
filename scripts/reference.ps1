param([string]$Path, [string]$Text)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$voice = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $voice.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::NotSet,
        [System.Speech.Synthesis.VoiceAge]::NotSet, 0, [System.Globalization.CultureInfo]::new('en-US'))
    $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
    $voice.SetOutputToWaveFile($Path, $format)
    $voice.Speak($Text)
} finally {
    $voice.Dispose()
}
