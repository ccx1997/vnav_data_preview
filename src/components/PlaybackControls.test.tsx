import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PlaybackControls } from './PlaybackControls'

describe('PlaybackControls', () => {
  it('reports timeline changes and toggles playback', () => {
    const onSeek = vi.fn()
    const onToggle = vi.fn()
    render(
      <PlaybackControls
        currentTime={0}
        duration={299}
        isPlaying={false}
        onToggle={onToggle}
        onSeek={onSeek}
      />,
    )

    fireEvent.change(screen.getByRole('slider', { name: '播放进度' }), { target: { value: '150' } })
    expect(onSeek).toHaveBeenCalledWith(150)
    fireEvent.click(screen.getByRole('button', { name: '播放' }))
    expect(onToggle).toHaveBeenCalledOnce()
  })
})
