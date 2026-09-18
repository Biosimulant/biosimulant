import { XIcon } from "@phosphor-icons/react";

export type AddToLabModalProps = {
  /** The lab on disk, so the commands below can be copied as-is. */
  labPath?: string | null;
  onCancel: () => void;
};

const DOCS_URL = "https://docs.biosimulant.com/references/cli";

function Command({ children }: { children: string }) {
  return (
    <pre className="add-to-lab-command">
      <code>{children}</code>
    </pre>
  );
}

export function AddToLabModal({ labPath, onCancel }: AddToLabModalProps) {
  const lab = labPath || "<lab>";
  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <form
        className="modal add-to-lab-modal"
        onClick={(event) => event.stopPropagation()}
        onSubmit={(event) => event.preventDefault()}
      >
        <div className="modal-header">
          <h2>Add to lab</h2>
          <button type="button" className="icon-button small" onClick={onCancel} title="Close">
            <XIcon size={14} />
          </button>
        </div>
        <div className="modal-body">
          <p className="muted small">
            This page edits and runs the lab that is already on disk. Add models and child labs
            with the <code>biosimulant</code> CLI, then refresh to pick them up.
          </p>

          <div className="add-to-lab-step">
            <p>
              <strong>A model already inside the lab folder</strong>
            </p>
            <Command>{`biosimulant labs add-model --lab ${lab} --alias growth models/growth`}</Command>
            <p className="muted small">The model path is relative to the lab.</p>
          </div>

          <div className="add-to-lab-step">
            <p>
              <strong>A model from somewhere else</strong>
            </p>
            <Command>{`biosimulant labs vendor-model --lab ${lab} --alias growth /path/to/model`}</Command>
            <p className="muted small">This copies it into the lab source tree.</p>
          </div>

          <div className="add-to-lab-step">
            <p>
              <strong>A published lab or model from the Hub</strong>
            </p>
            <Command>{`biosimulant labs search glycolysis
biosimulant labs pull demi/bakker2001-glycolysis@1.0.0 --target ./downloaded`}</Command>
            <p className="muted small">
              Then vendor it in with the command above. Public packages need no sign-in.
            </p>
          </div>

          <p className="muted small">
            <a href={DOCS_URL} target="_blank" rel="noreferrer">
              Full CLI reference →
            </a>
          </p>
        </div>
        <div className="modal-footer">
          <button type="button" className="button" onClick={onCancel}>
            Close
          </button>
        </div>
      </form>
    </div>
  );
}
