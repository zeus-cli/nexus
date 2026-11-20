//! Identidade e permissões do Control Plane (T8).
//!
//! O catálogo versionado (`contracts/identity/*.json`) é embutido no binário e
//! validado uma vez. O `IdentityStore` persiste principals, papéis,
//! credenciais, devices e pairing no mesmo SQLite do T6, com schema versionado.

pub mod catalog;
pub mod store;

pub use store::IdentityStore;
