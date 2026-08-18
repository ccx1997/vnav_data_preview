import { formatClock } from '../lib/playback'

interface PlaybackControlsProps {
  currentTime: number
  duration: number
  isPlaying: boolean
  onToggle: () => void
  onSeek: (seconds: number) => void
}

export function PlaybackControls({
  currentTime,
  duration,
  isPlaying,
  onToggle,
  onSeek,
}: PlaybackControlsProps) {
  const progress = duration > 0 ? Math.min(100, Math.max(0, (currentTime / duration) * 100)) : 0
  return (
    <footer className="playback-bar">
      <button className="play-button" type="button" onClick={onToggle} aria-label={isPlaying ? '暂停' : '播放'}>
        {isPlaying ? (
          <span className="pause-icon" aria-hidden="true"><i /><i /></span>
        ) : (
          <span className="play-icon" aria-hidden="true" />
        )}
      </button>
      <div className="time-display time-display--current">{formatClock(currentTime)}</div>
      <div className="timeline-wrap">
        <input
          className="timeline"
          type="range"
          min="0"
          max={Math.max(duration, 0)}
          step="0.01"
          value={Math.min(currentTime, duration || 0)}
          onChange={(event) => onSeek(Number(event.target.value))}
          aria-label="播放进度"
          style={{ '--progress': `${progress}%` } as React.CSSProperties}
        />
      </div>
      <div className="time-display">{formatClock(duration)}</div>
      <span className="speed-chip">1×</span>
    </footer>
  )
}
