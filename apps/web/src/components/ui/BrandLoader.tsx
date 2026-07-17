/* BrandLoader — the first-paint loader. A small moon-moth that flits in place
 * while a view waits for its first hub.state snapshot, so the board/deck no
 * longer flash an "empty" state (and then the first-run overlay) before the
 * roster has even loaded.
 *
 * It is deliberately featherweight: one inline SVG, a transform-only CSS
 * animation (GPU-cheap), and a DELAYED fade-in (.brand-loader .mk in
 * global.css) — if the snapshot comes back fast the loader unmounts before it
 * ever paints, so we never trade a blank flash for a loader flash. The mark
 * mirrors the product wordmark (OpenCharaAgent = 月蛾); reduced-motion drops the flit.
 */
export function BrandLoader() {
  return (
    <div className="brand-loader" role="status" aria-live="polite" aria-busy="true">
      <svg className="mk" viewBox="262 290 500 520" fill="currentColor" role="img" aria-label="OpenCharaAgent">
        <defs>
          <g id="bl-half">
            <path d="M 506 402 C 490 360 470 328 442 308 C 470 318 496 352 510 394 Z" />
            <path d="M 507 436 C 460 408 380 360 318 342 C 290 334 276 344 280 366 C 288 412 330 472 394 508 C 442 535 486 542 504 532 Z" />
            <path d="M 503 542 C 455 538 400 556 386 596 C 372 634 392 666 424 672 C 420 716 426 756 444 788 C 450 798 462 796 464 784 C 470 730 476 670 486 626 C 492 596 500 560 503 548 Z" />
          </g>
        </defs>
        <g transform="rotate(-8 512 530)">
          <use href="#bl-half" />
          <use href="#bl-half" transform="matrix(-1,0,0,1,1024,0)" />
          <path d="M 512 404 C 530 404 538 426 537 458 C 536 510 526 562 512 588 C 498 562 488 510 487 458 C 486 426 494 404 512 404 Z" />
          <circle cx="512" cy="404" r="19" />
        </g>
      </svg>
    </div>
  );
}
