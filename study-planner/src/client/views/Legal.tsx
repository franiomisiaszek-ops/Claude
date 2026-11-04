import { useEffect, useState } from 'react';
import { APP_NAME } from '../../shared/brand';

function useContact() {
  const [contact, setContact] = useState<string | null>(null);
  useEffect(() => {
    fetch('/api/config')
      .then((r) => r.json())
      .then((c) => setContact(c.supportEmail ?? null))
      .catch(() => {});
  }, []);
  return contact;
}

function Shell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="legal">
      <a className="brand" href="/">
        <img src="/favicon.svg" alt="" width={28} height={28} />
        <span>{APP_NAME}</span>
      </a>
      <article className="card legal-body">
        <h1>{title}</h1>
        {children}
      </article>
    </div>
  );
}

export function Privacy() {
  const contact = useContact();
  return (
    <Shell title="Privacy Policy">
      <p className="muted small">Last updated: November 2026</p>
      <p>
        {APP_NAME} helps you plan study time. This policy explains what we collect, why, and the choices you have. We do not sell your data, show ads, or use
        tracking cookies.
      </p>
      <h2>What we collect</h2>
      <ul>
        <li>
          <strong>Account:</strong> your name, email address and a securely hashed password, or your Google account name and email if you sign in with Google.
        </li>
        <li>
          <strong>Study data you add:</strong> courses, exams, deadlines, study material, availability, study sessions, and the grades you choose to enter.
        </li>
        <li>
          <strong>Connected apps (read-only, only if you connect them):</strong> from Google Classroom, your courses, coursework, course materials and whether you
          turned work in; from Google Calendar, event times and titles from the calendars you pick; from calendar feed links you paste, the events in that feed.
        </li>
        <li>
          <strong>Documents you upload for analysis:</strong> processed to count pages and find chapters, then discarded. We keep only the resulting list of study
          units.
        </li>
        <li>
          <strong>Notifications:</strong> if you turn on reminders, your browser's push subscription address.
        </li>
        <li>
          <strong>Technical data:</strong> IP address and browser type in your login sessions and server logs, used for security.
        </li>
      </ul>
      <h2>How we use it</h2>
      <p>Only to provide the service: building and updating your study plan, keeping it in sync with apps you connect, sending the reminders you turn on, and keeping your account secure.</p>
      <h2>Google user data</h2>
      <p>
        {APP_NAME}'s use and transfer of information received from Google APIs adheres to the{' '}
        <a href="https://developers.google.com/terms/api-services-user-data-policy" target="_blank" rel="noreferrer">
          Google API Services User Data Policy
        </a>
        , including the Limited Use requirements. We use Google data only to show and plan your coursework and calendar inside {APP_NAME}. We never use it for
        advertising, never sell it, never let humans read it except with your permission, for security, or as required by law, and never use it to train AI models.
      </p>
      <h2>Service providers</h2>
      <p>We use a small number of processors to run the service, under agreements that limit them to processing on our behalf:</p>
      <ul>
        <li>Hosting and database providers, which store your account and study data.</li>
        <li>An email provider, only if email reminders or account emails are turned on.</li>
        <li>
          Anthropic (Claude), only when you ask the app to analyze a document or syllabus. We send the text of that document with contact details removed. Anthropic
          does not use API data to train its models.
        </li>
      </ul>
      <h2>Cookies</h2>
      <p>We use one strictly necessary cookie to keep you signed in. No analytics or advertising cookies.</p>
      <h2>Retention and deletion</h2>
      <p>
        Your data stays until you delete it. You can download all of it as a file, or delete your account, at any time in Settings. Deleting your account removes
        your data from our database immediately and revokes our access to Google. Backups expire within 30 days.
      </p>
      <h2>Children and students</h2>
      <p>
        {APP_NAME} is for people aged 13 and older; younger users need a parent's permission. School-managed Google accounts for students under 18 can only
        connect if the school's administrator allows the app.
      </p>
      <h2>Your rights</h2>
      <p>Depending on where you live (for example under the GDPR in the EU/UK), you can access, correct, export or delete your data and object to processing. Use the tools in Settings or contact us.</p>
      <h2>Contact</h2>
      <p>{contact ? <a href={`mailto:${contact}`}>{contact}</a> : 'Contact details are provided by the operator of this site.'}</p>
    </Shell>
  );
}

export function Terms() {
  const contact = useContact();
  return (
    <Shell title="Terms of Use">
      <p className="muted small">Last updated: November 2026</p>
      <p>By creating an account you agree to these terms.</p>
      <h2>The service</h2>
      <p>
        {APP_NAME} suggests study plans based on the material and dates you provide or connect. Plans and time estimates are suggestions; you remain responsible
        for checking real deadlines and requirements with your school or teachers.
      </p>
      <h2>Your account</h2>
      <ul>
        <li>You must be at least 13, or have a parent's or guardian's permission.</li>
        <li>Keep your password safe. You're responsible for activity in your account.</li>
        <li>Only upload documents you have the right to use.</li>
      </ul>
      <h2>Acceptable use</h2>
      <p>Don't misuse the service: no attempts to break security, overload it, access other people's data, or use it for anything illegal.</p>
      <h2>AI features</h2>
      <p>Document and syllabus analysis is automated and can be wrong. Nothing it suggests is added to your plan until you review and accept it.</p>
      <h2>Availability and changes</h2>
      <p>We work to keep the service running but can't guarantee it will always be available. We may change features or these terms and will tell you about important changes.</p>
      <h2>Ending your account</h2>
      <p>You can delete your account at any time in Settings. We may suspend accounts that break these terms.</p>
      <h2>Liability</h2>
      <p>The service is provided "as is". To the extent the law allows, we aren't liable for indirect losses, such as missed deadlines or grades.</p>
      <h2>Contact</h2>
      <p>{contact ? <a href={`mailto:${contact}`}>{contact}</a> : 'Contact details are provided by the operator of this site.'}</p>
    </Shell>
  );
}
