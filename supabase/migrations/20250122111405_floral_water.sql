/*
  # Add insert policy for profiles table

  1. Security
    - Add policy to allow authenticated users to insert their own profile
*/

CREATE POLICY "Users can insert their own profile"
  ON profiles FOR INSERT
  TO authenticated
  WITH CHECK (auth.uid() = id);