export class LiveAudioPlayer {
  private context: AudioContext | null = null;
  private nextTime = 0;
  private sources = new Set<AudioBufferSourceNode>();
  private gain: GainNode | null = null;
  private volume = 0.8;
  async prepare() {
    if (!this.context || this.context.state === "closed")
      this.context = new AudioContext();
    if (!this.gain) {
      this.gain = this.context.createGain();
      this.gain.gain.value = this.volume;
      this.gain.connect(this.context.destination);
    }
    if (this.context.state === "suspended") await this.context.resume();
  }
  append(base64: string, sampleRate: number, channels = 1) {
    const context = this.context;
    if (!context || context.state !== "running" || !base64 || channels < 1)
      return;
    const bytes = Uint8Array.from(atob(base64), (character) =>
      character.charCodeAt(0),
    );
    const samples = Math.floor(bytes.length / 2 / channels);
    if (!samples) return;
    const buffer = context.createBuffer(channels, samples, sampleRate);
    const view = new DataView(bytes.buffer);
    for (let channel = 0; channel < channels; channel++) {
      const output = buffer.getChannelData(channel);
      for (let index = 0; index < samples; index++)
        output[index] =
          view.getInt16((index * channels + channel) * 2, true) / 32768;
    }
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.gain ?? context.destination);
    this.sources.add(source);
    source.onended = () => {
      source.disconnect();
      this.sources.delete(source);
    };
    const start = Math.max(context.currentTime + 0.035, this.nextTime);
    source.start(start);
    this.nextTime = start + buffer.duration;
  }
  stop() {
    for (const source of this.sources) {
      try {
        source.stop();
      } catch {
        /* Already ended. */
      }
      source.disconnect();
    }
    this.sources.clear();
    this.nextTime = 0;
  }
  setVolume(value: number) {
    this.volume = Math.max(0, Math.min(1, value));
    if (this.gain) this.gain.gain.value = this.volume;
  }
  close() {
    this.stop();
    if (this.context) void this.context.close();
    this.context = null;
    this.gain = null;
  }
}
