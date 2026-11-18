import { useEffect, useRef } from "react";
import WaveSurfer from "wavesurfer.js";

type Peaks = { data: Float32Array; duration: number };
const peakCache = new Map<string, Promise<Peaks>>();
let decoder: AudioContext | undefined;
let peakWorker: Worker | undefined;
let nextRequest = 0;
const pending = new Map<number, { resolve: (value: Peaks) => void; reject: (error: Error) => void }>();
function reducePeaks(samples: Float32Array, duration: number): Promise<Peaks> {
  if (!peakWorker) {
    peakWorker = new Worker(new URL("./peakWorker.ts", import.meta.url), { type: "module" });
    peakWorker.onmessage = ({ data }: MessageEvent<Peaks & { id: number }>) => {
      pending.get(data.id)?.resolve({ data: data.data, duration: data.duration });
      pending.delete(data.id);
    };
    peakWorker.onerror = () => {
      for (const task of pending.values()) task.reject(new Error("波形计算失败"));
      pending.clear();
      peakWorker?.terminate();
      peakWorker = undefined;
    };
  }
  const id = ++nextRequest;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    peakWorker!.postMessage({ id, samples, duration }, [samples.buffer]);
  });
}
function getPeaks(url: string, key: string) {
  const existing = peakCache.get(key);
  if (existing) return existing;
  const request = (async () => {
    const response = await fetch(url);
    if (!response.ok) throw new Error("无法读取波形");
    decoder ??= new AudioContext();
    const audio = await decoder.decodeAudioData(await response.arrayBuffer());
    // Large clips are reduced off the UI thread; only small cached peaks are
    // handed to WaveSurfer. DecodeAudioData itself is asynchronous.
    return reducePeaks(audio.getChannelData(0).slice(), audio.duration);
  })();
  peakCache.set(key, request);
  void request.catch(() => peakCache.delete(key));
  if (peakCache.size > 160) peakCache.delete(peakCache.keys().next().value!);
  return request;
}

export function Waveform({
  url,
  height = 42,
  color = "#63b6a9",
  compact = false,
  start = 0,
  end,
}: {
  url: string;
  height?: number;
  color?: string;
  compact?: boolean;
  start?: number;
  end?: number;
}) {
  const host = useRef<HTMLDivElement>(null);
  const latestUrl = useRef(url);
  latestUrl.current = url;
  const cacheKey = url.split("?")[0];
  useEffect(() => {
    const container = host.current;
    if (!container || !latestUrl.current) return;
    let wave: WaveSurfer | undefined;
    let disposed = false;
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      observer.disconnect();
      void getPeaks(latestUrl.current, cacheKey)
        .then((peaks) => {
          if (disposed) return;
          const from = Math.max(
            0,
            Math.floor((start / peaks.duration) * peaks.data.length),
          );
          const to = Math.min(
            peaks.data.length,
            Math.ceil(
              ((end ?? peaks.duration) / peaks.duration) * peaks.data.length,
            ),
          );
          const data = peaks.data.slice(from, Math.max(from + 1, to));
          wave = WaveSurfer.create({
            container,
            height,
            waveColor: color,
            progressColor: color,
            cursorWidth: 0,
            barWidth: compact ? 2 : 2,
            barGap: 2,
            barRadius: 1,
            normalize: true,
            interact: false,
            hideScrollbar: true,
            fillParent: true,
            peaks: [data],
            duration: Math.max(0.001, (end ?? peaks.duration) - start),
          });
        })
        .catch(() => undefined);
    });
    observer.observe(container);
    return () => {
      disposed = true;
      observer.disconnect();
      wave?.destroy();
    };
  }, [cacheKey, height, color, compact, start, end]);
  return <div className="waveform" ref={host} />;
}
