class StudioPCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.gate = {capture: false, generation: 0};
    this.port.onmessage = ({data}) => {
      this.gate = data;
      this.seq = 0;
      this.position = 8;
      this.phase = 0;
      this.sum = 0;
      this.count = 0;
      this.packet = new ArrayBuffer(3208);
    };
  }

  process(inputs) {
    if (!this.gate.capture) return true;
    const channels = inputs[0];
    if (!channels?.length) return true;
    for (let index = 0; index < channels[0].length; index++) {
      let value = 0;
      for (const channel of channels) value += channel[index];
      this.sum += value / channels.length;
      this.count++;
      this.phase += 16000;
      if (this.phase < sampleRate) continue;
      this.phase -= sampleRate;
      const sample = Math.max(-1, Math.min(1, this.sum / this.count));
      new DataView(this.packet).setInt16(this.position, sample * 32767, true);
      this.position += 2;
      this.sum = this.count = 0;
      if (this.position === 3208) {
        const header = new DataView(this.packet);
        header.setUint32(0, this.gate.generation, true);
        header.setUint32(4, this.seq++, true);
        this.port.postMessage(this.packet, [this.packet]);
        this.packet = new ArrayBuffer(3208);
        this.position = 8;
      }
    }
    return true;
  }
}

registerProcessor("studio-pcm", StudioPCM);
