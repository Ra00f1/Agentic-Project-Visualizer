import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SchemaFields } from "./SchemaFields";

afterEach(() => {
  cleanup();
});

describe("SchemaFields", () => {
  it("renders a string field as a text input, pre-filled from the default", () => {
    render(
      <SchemaFields
        schema={{ properties: { path: { type: "string", title: "Trace log path", default: "a.jsonl" } } }}
        values={{}}
        onChange={vi.fn()}
      />,
    );
    const input = screen.getByLabelText("Trace log path") as HTMLInputElement;
    expect(input).toHaveAttribute("type", "text");
    expect(input.value).toBe("a.jsonl");
  });

  it("renders an integer field as a number input", () => {
    render(
      <SchemaFields
        schema={{ properties: { maxCalls: { type: "integer", title: "Max calls", default: 10 } } }}
        values={{}}
        onChange={vi.fn()}
      />,
    );
    const input = screen.getByLabelText("Max calls") as HTMLInputElement;
    expect(input).toHaveAttribute("type", "number");
    expect(input.value).toBe("10");
  });

  it("renders a boolean field as a checkbox", () => {
    render(
      <SchemaFields
        schema={{ properties: { verbose: { type: "boolean", title: "Verbose", default: true } } }}
        values={{}}
        onChange={vi.fn()}
      />,
    );
    const checkbox = screen.getByLabelText("Verbose") as HTMLInputElement;
    expect(checkbox).toHaveAttribute("type", "checkbox");
    expect(checkbox.checked).toBe(true);
  });

  it("shows an unsupported-type notice for anything outside string/integer/boolean, without crashing", () => {
    render(
      <SchemaFields
        schema={{ properties: { tags: { type: "array", title: "Tags" } } }}
        values={{}}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText("Tags: unsupported setting type (array)")).toBeInTheDocument();
  });

  it("falls back to the field name when a property entry is malformed", () => {
    render(
      <SchemaFields
        schema={{ properties: { weird: "not-an-object" } }}
        values={{}}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText("weird: unsupported setting type")).toBeInTheDocument();
  });

  it("calls onChange with the field name and new value when the user types", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <SchemaFields
        schema={{ properties: { path: { type: "string", title: "Trace log path", default: "" } } }}
        values={{}}
        onChange={onChange}
      />,
    );

    await user.type(screen.getByLabelText("Trace log path"), "x");

    expect(onChange).toHaveBeenCalledWith("path", "x");
  });

  it("prefers a value already in `values` over the schema's own default", () => {
    render(
      <SchemaFields
        schema={{ properties: { path: { type: "string", title: "Trace log path", default: "default.jsonl" } } }}
        values={{ path: "custom.jsonl" }}
        onChange={vi.fn()}
      />,
    );
    expect((screen.getByLabelText("Trace log path") as HTMLInputElement).value).toBe("custom.jsonl");
  });

  it("renders nothing when the schema has no properties", () => {
    const { container } = render(<SchemaFields schema={{}} values={{}} onChange={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });
});
