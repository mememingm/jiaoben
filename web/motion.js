(() => {
  if (typeof gsap === "undefined") return;

  const media = gsap.matchMedia();
  media.add("(prefers-reduced-motion: no-preference)", () => {
    const sections = gsap.utils.toArray([
      ".control-panel > .panel",
      ".control-panel > .safety-note",
      ".data-panel > .metrics-grid",
      ".data-panel > .query-quality:not([hidden])",
      ".data-panel > .table-container",
      ".activation-main > .activation-hero",
      ".activation-main > .activation-config-panel",
      ".activation-main > .platform-workspace-grid",
      ".activation-main > .activation-workflow-panel",
      ".activation-main > .activation-safety",
    ].join(", "));

    gsap.timeline({ defaults: { ease: "power2.out" } })
      .from(".top-nav", { y: -8, autoAlpha: 0, duration: 0.3, clearProps: "transform,opacity,visibility" })
      .from(sections, {
        y: 10,
        autoAlpha: 0,
        duration: 0.38,
        stagger: 0.055,
        clearProps: "transform,opacity,visibility",
      }, "<+0.08");
  });

  window.addEventListener("pagehide", () => media.revert(), { once: true });
})();
