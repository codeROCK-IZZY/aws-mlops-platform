import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { AuthProvider } from "../lib/auth";
import { LoginPage } from "../pages/LoginPage";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("LoginPage", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows the server's error message on failed login", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/auth/refresh")) return json(401, { detail: "Missing refresh token" });
      return json(401, { detail: "Invalid email or password" });
    });
    render(<MemoryRouter><AuthProvider><LoginPage /></AuthProvider></MemoryRouter>);
    await userEvent.type(screen.getByLabelText("Email"), "a@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Invalid email or password"));
  });
});
