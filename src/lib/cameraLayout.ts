export interface PositionedCamera {
  id: string
  positionZh: string
}

const CAMERA_POSITION_SLOTS = [
  ['后', '后左'],
  ['前上'],
  ['前广', '右中', '后上'],
  ['左'],
  ['前下'],
  ['右'],
] as const

export function arrangeCameras<T extends PositionedCamera>(cameras: T[]): Array<T | null> {
  const unused = new Set(cameras.map((camera) => camera.id))
  const slots: Array<T | null> = CAMERA_POSITION_SLOTS.map((positionNames) => {
    const camera = cameras.find(
      (candidate) =>
        unused.has(candidate.id) && positionNames.some((positionName) => positionName === candidate.positionZh.trim()),
    ) ?? null
    if (camera) unused.delete(camera.id)
    return camera
  })

  const unassigned = cameras.filter((camera) => unused.has(camera.id))
  for (const slotIndex of [0, 2]) {
    if (!slots[slotIndex] && unassigned.length) slots[slotIndex] = unassigned.shift() ?? null
  }

  return slots
}
