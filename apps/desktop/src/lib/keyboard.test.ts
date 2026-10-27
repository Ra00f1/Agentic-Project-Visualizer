import { describe, expect, it } from "vitest";
import { isTypingInField } from "./keyboard";

describe("isTypingInField", () => {
  it("returns true for an input element", () => {
    const el = document.createElement("input");
    expect(isTypingInField(el)).toBe(true);
  });

  it("returns true for a textarea element", () => {
    const el = document.createElement("textarea");
    expect(isTypingInField(el)).toBe(true);
  });

  it("returns true for a contenteditable element", () => {
    // jsdom doesn't implement `isContentEditable` at all (always
    // `undefined`, even with the `contenteditable` attribute set and the
    // element attached to the document) -- real browsers do, per MDN.
    // Stub the getter directly so this test exercises the function's own
    // branch rather than jsdom's gap.
    const el = document.createElement("div");
    Object.defineProperty(el, "isContentEditable", { value: true });
    expect(isTypingInField(el)).toBe(true);
  });

  it("returns false for a button", () => {
    const el = document.createElement("button");
    expect(isTypingInField(el)).toBe(false);
  });

  it("returns false for a plain div", () => {
    const el = document.createElement("div");
    expect(isTypingInField(el)).toBe(false);
  });

  it("returns false for document.body", () => {
    expect(isTypingInField(document.body)).toBe(false);
  });

  it("returns false for null", () => {
    expect(isTypingInField(null)).toBe(false);
  });

  it("returns false for a non-HTMLElement EventTarget", () => {
    // window is a valid keydown event target but not an HTMLElement --
    // the instanceof check must reject it, not throw.
    expect(isTypingInField(window)).toBe(false);
  });
});
