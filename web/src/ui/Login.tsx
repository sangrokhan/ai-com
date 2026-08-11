import { useState } from "react";

import { login } from "../api/client";

export function Login() {
  const [password, setPassword] = useState("");
  const [failed, setFailed] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (await login(password)) window.location.reload();
    else setFailed(true);
  };

  return (
    <form className="login" onSubmit={submit}>
      <h1>ai-com console</h1>
      <input
        type="password"
        value={password}
        autoFocus
        onChange={(e) => setPassword(e.target.value)}
        placeholder="password"
      />
      <button type="submit">enter</button>
      {failed && <p className="error">wrong password</p>}
    </form>
  );
}
