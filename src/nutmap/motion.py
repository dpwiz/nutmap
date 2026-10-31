import numpy as np


def logistic(x):
    return 1 / (1 + np.exp(-x))


def sigmoid_ease(t, steepness):
    lo, hi = logistic(-steepness / 2), logistic(steepness / 2)
    return (logistic(steepness * (t - 0.5)) - lo) / (hi - lo)


def eased_alphas(count, steepness):
    t = np.linspace(0, 1, count)
    alphas = sigmoid_ease(t, steepness) if steepness > 0 else t
    alphas[0], alphas[-1] = 0.0, 1.0
    return alphas


def normalise(xy):
    xy = xy - xy.mean(0)
    return xy / np.sqrt((xy**2).sum(1).mean())


def best_rotation(moving, fixed):
    u, _, vt = np.linalg.svd(moving.T @ fixed)
    if np.linalg.det(u @ vt) < 0:
        u[:, -1] *= -1
    return u @ vt


def align(xy, target):
    xy = normalise(xy)
    return xy @ best_rotation(xy, target)


def procrustes_chain(layouts):
    chained = [normalise(layouts[0])]
    turns = []
    for layout in layouts[1:]:
        current = normalise(layout)
        rotation = best_rotation(current, chained[-1])
        turns.append(np.degrees(np.arctan2(rotation[1, 0], rotation[0, 0])))
        chained.append(current @ rotation)
    return np.stack(chained), np.abs(turns)


def steadied(sweep, sigma):
    from scipy.ndimage import gaussian_filter1d

    chained, _ = procrustes_chain(sweep.astype(np.float64))
    if sigma <= 0:
        return chained
    smooth = gaussian_filter1d(chained, sigma=sigma, axis=0, mode="nearest")
    smooth[0] = chained[0]
    return smooth
