---
version: v39
note: 필요한 시점에만 읽는 시각 점검 절차. scene 진입이나 screen capture마다 점검을 요구하지 않는다.
placeholders: []
description: Visual inspection of layout, missing art, overlap and clipping. Load when a TC requires appearance checks, a visual defect is suspected, or exploratory QA needs a visual check.
---
# Visual inspection

Use this procedure when a TC needs visual evidence, the screen suggests a visual defect, or exploratory QA calls for a visual check. Entering a scene or taking an unrelated screenshot does not require this procedure. Keep the current TC's goal first.

1. Identify the expected appearance from the TC, an available design reference, or the surrounding UI. Do not invent an expected design. If the intended appearance is unclear, gather more context or ask the user rather than declaring a defect.
2. Read a current screen capture. With `capture_screen`, choose the whole screen for layout or the relevant region for a local detail. If images already arrive with tool results, use the current image; request another only if it is stale or insufficient. Scene listings describe objects, not their rendered appearance.
3. Inspect only the relevant details: unexpectedly empty or black areas, missing art, overlapping elements, text or images clipped by boundaries, unreadable text, and error or developer overlays. A loading state, intentional empty area, or expected overlay alone is not proof of a defect.
4. If needed, wait for loading or animation to settle, reproduce the state, or compare before and after an interaction. Record what you actually observed and the image or state supporting it. Do not repeat captures when they cannot resolve the uncertainty.
5. If the defect contradicts this TC's expected result, use it as evidence in `report_test_case`; the server manages the TC failure issue. If it is a separate defect outside the TC, use `report_issue` with reproduction steps and evidence. Avoid reporting the same defect in both paths. Inconclusive appearance is not a confirmed failure.

Return to the TC after the relevant check. This skill does not require inspecting every scene or every image.
