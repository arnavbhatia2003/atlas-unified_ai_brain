# Atlas frontend

React, TypeScript, Vite, Tailwind CSS, and the existing UI primitives.
The dark industrial interface and public landing page come from Sukhesh's Atlas
repository; the API and operational workflows use Arnav's Atlas backend.

From this folder:

```powershell
npm install
# First setup only: copy .env.example .env
npm run dev
```

Open `http://localhost:5173` for the landing page or `/home` for the dashboard.
The backend defaults to `http://localhost:8001`; set `VITE_API_URL` in `.env`
to use another backend. See the root README for backend setup and demo data.

Checks: `npm run lint` and `npm run build`.
