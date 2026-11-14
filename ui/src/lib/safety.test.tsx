import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { safeExternalUrl } from "./links";
import { Markdown } from "./markdown";

describe("external links", () => {
  it("allows https to listed hosts and strips query strings and fragments", () => {
    expect(safeExternalUrl("https://github.com/org/repo/pull/1?token=abc#x")).toEqual({
      href: "https://github.com/org/repo/pull/1",
      host: "github.com",
    });
  });

  it.each([
    "http://github.com/x",
    "javascript:alert(1)",
    "data:text/html,hi",
    "https://attacker.example/?evidence=secret",
    "https://github.com.attacker.example/",
    ["https://u:p", "github.com/"].join("@"),
    "https://github.com:8443/",
    "not a url",
  ])("refuses %s", (url) => {
    expect(safeExternalUrl(url)).toBeNull();
  });
});

describe("Markdown", () => {
  it("never renders raw HTML", () => {
    const { container } = render(<Markdown source={'# Title\n\n<script>alert(1)</script> <img src=x onerror=alert(1)>'} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<script>alert(1)</script>");
  });

  it("shows a disallowed link as plain text, not a link", () => {
    const { container } = render(<Markdown source="See [this](https://attacker.example/?q=secret)." />);
    expect(container.querySelector("a")).toBeNull();
    expect(screen.getByText("this").className).toBe("link-blocked");
  });

  it("asks before leaving and opens an allowed link without referrer or query", () => {
    render(<Markdown source="See [the PR](https://github.com/o/r/pull/2?x=1)." />);
    fireEvent.click(screen.getByRole("button", { name: "the PR" }));
    const a = screen.getByRole("link");
    expect(a.getAttribute("href")).toBe("https://github.com/o/r/pull/2");
    expect(a.getAttribute("rel")).toContain("noreferrer");
    expect(a.getAttribute("rel")).toContain("noopener");
  });

  it("renders lists, bold and code", () => {
    const { container } = render(<Markdown source={"- **one** and `two`\n- three"} />);
    expect(container.querySelectorAll("li")).toHaveLength(2);
    expect(container.querySelector("strong")?.textContent).toBe("one");
    expect(container.querySelector("code")?.textContent).toBe("two");
  });
});
